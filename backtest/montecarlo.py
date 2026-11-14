"""
montecarlo.py — 서서히 백테스트: 3개 전략 비교 + 20종목×10회 무작위 바스켓

[무엇을 하나]
공통 백테스트 엔진(engine.run_strategy) 위에서 세 전략의 자산가치 곡선 W_t 를 만들고,
120종목 중 20종목을 무작위로 뽑아 10번 돌려 결과의 분포(강건성)를 본다.

[세 전략]
    무손절   : 끝까지 보유 (sell_schedule = {})
    단순손절 : 진입 대비 누적수익률이 -STOP_PCT 밑으로 떨어진 첫 거래일에 매도
    서서히   : 매 기준일마다 run_seoseohi_pit 로 재평가, 판정이 TRIGGER(기본 'Yes(훼손 우려)')
               에 처음 걸리는 기준일(다음 거래일로 스냅)에 매도

[팀 결정]
    매도 트리거 : 기본 'Yes(훼손 우려)'만. TRIGGER 에 '주의'를 추가하면 민감하게 바뀐다
    재평가 시점 : 매 기준일(21개 스냅샷). 새 재무가 들어오는 순간이라 판정이 바뀔 수 있는 유일한 지점
    매수일      : 백테스트 시작일(기본 2016-01-04). 모든 종목을 이 날 산 것으로 보고 평가일과 비교
"""

from pathlib import Path
import numpy as np
import pandas as pd

from models.seoseohi.pipeline import run_seoseohi_pit, load_ksic_from_base, integrate, integrate_sum
from models.seoseohi.fscore import list_snapshot_dates
from backtest.engine import run_strategy, INITIAL_CASH, CASH_DAILY_RATE
from backtest.metrics import cumret, mdd, sortino, sharpe, cash_effect_summary

DEFAULT_TRIGGER = {"Yes(훼손 우려)"}
DEFAULT_STOP_PCT = 0.20              # 단순손절 기준: -20%

# ── 두 날짜를 구분한다 (예전엔 하나로 합쳐 버그가 났다) ──────────────────
# entry_date  : 실제 매수일(엔진 보유 시작). 단순손절은 이 날 대비 낙폭을 잰다.
# baseline_date: 서서히 '기준선'. 매수 직후엔 아직 점수화할 재무가 없어서(첫 스냅샷
#                2015-12 는 지원파일이 비어 FY2014 를 못 매긴다), 매수 후 처음으로
#                점수가 나오는 기준일(FY2015 공시분 = 2016-04-01)을 기준선으로 쓴다.
#                "1월에 샀고, 첫 사업보고서(≈4월)부터 재무 훼손을 지켜본다"는 의미.
DEFAULT_ENTRY_DATE = "2016-01-04"
DEFAULT_BASELINE_DATE = "2016-04-01"
DEFAULT_GATE_LOOKBACK = 120         # 가격 게이트: 최근 120 거래일(약 6개월) 추세를 본다


#  가격 게이트 (threshold 모델의 간단한 대체물) — 기본 OFF (실데이터 검증상 역효과)
# [검증] 21스냅샷 실데이터에서 최근 120일(6개월) 추세 게이트는 현금대체를 악화시켰다:
#   이 종목군은 평균회귀가 강해 "6개월 하락"이 '더 하락'이 아니라 '반등'을 예고한다.
#   → 게이트를 켜면 가장 많이 빠진(가장 잘 튀는) 종목만 골라 팔게 되어 손해.
#   진짜 '하락 추세' 필터는 각 종목의 평소 변동성 대비로 보는 threshold(CUSUM) 모델의 몫.
#   참고: 이벤트 스터디상 신호는 ~2년 지평에서만 맞았으므로 lookback 을 길게(≈500일) 주면
#   결과가 달라질 수 있다(실험용). 기본값은 끈 상태로 둔다.
# "떨어지고 있는가?" — 서서히(재무)가 Yes 여도, 가격이 이미 반등 중이면 팔지 않는다.
def is_falling(grid, stock, date, lookback=DEFAULT_GATE_LOOKBACK):
    """date 까지 최근 lookback 거래일 누적수익률이 음수면 '아직 하락 중'(=매도 confirm).
    회복 중(>=0)이면 False → 매도 보류."""
    hist = grid[stock].loc[:pd.Timestamp(date)].dropna()
    if len(hist) == 0:
        return False
    window = hist.iloc[-lookback:]
    trailing = (1 + window / 100).prod() - 1
    return trailing < 0


#  점수 캐시 (속도) — (base, 종목, 기준선, 평가일) 세 모델 점수는 결정적이라 한 번만 계산.
# 두 판정 방식(개수/합산)이 같은 캐시를 공유한다 (판정은 점수에서 그때그때 뽑는다).
_VERDICT_CACHE = {}   # 이름은 유지하되 '판정 문자열'이 아니라 '세 모델 점수'를 저장


def _scores(base_dir, ksic, stock, baseline_date, eval_date):
    key = (str(base_dir), stock, str(baseline_date), str(eval_date))
    if key not in _VERDICT_CACHE:
        data = {"base_dir": str(base_dir), "ksic": ksic}
        _VERDICT_CACHE[key] = run_seoseohi_pit(
            data, {"stock_code": stock, "buy_date": baseline_date}, eval_date)["scores"]
    return _VERDICT_CACHE[key]


def _verdict(base_dir, ksic, stock, baseline_date, eval_date, mode="count"):
    scores = _scores(base_dir, ksic, stock, baseline_date, eval_date)
    return integrate(scores) if mode == "count" else integrate_sum(scores)


def clear_verdict_cache():
    _VERDICT_CACHE.clear()


#  전략별 매도 스케줄

def stoploss_schedule(basket, stop_pct=DEFAULT_STOP_PCT, start=DEFAULT_ENTRY_DATE, end_date=None):
    """단순손절: 진입(entry) 대비 누적수익률이 -stop_pct 밑으로 처음 내려간 거래일에 매도."""
    grid = basket[basket.index >= start]
    cum = (1 + grid.fillna(0) / 100).cumprod()
    schedule = {}
    for s in grid.columns:
        hit = cum.index[cum[s] <= (1 - stop_pct)]
        if len(hit) > 0:
            schedule[s] = hit[0]
    if end_date is not None:
        schedule = {s: d for s, d in schedule.items() if d <= pd.Timestamp(end_date)}
    return schedule

def seoseohi_schedule(base_dir, ksic, stocks, grid,
                      baseline_date=DEFAULT_BASELINE_DATE, trigger=DEFAULT_TRIGGER,
                      end_date=None, price_gate=True, gate_lookback=DEFAULT_GATE_LOOKBACK,
                      mode="count"):
    """서서히: 기준선(baseline_date) 이후 매 기준일마다 재평가, 판정이 trigger 에 처음
    걸리는 기준일에 매도. 기준일은 휴장일일 수 있으므로 '그 날짜 이상인 첫 거래일'로 스냅한다.
    (판정은 baseline_date 재무 vs 평가일 재무를 비교한다 → run_seoseohi_pit 의 buy_date).
    price_gate=True 면 '재무 Yes' 에 더해 '가격도 하락 중'일 때만 판다(반등주 거르기)."""
    eval_dates = [d for d in list_snapshot_dates(base_dir)
                  if pd.Timestamp(d) > pd.Timestamp(baseline_date)
                  and (end_date is None or pd.Timestamp(d) <= pd.Timestamp(end_date))]
    trading_days = grid.index
    schedule = {}
    for s in stocks:
        for d in eval_dates:
            verdict = _verdict(base_dir, ksic, s, baseline_date, d, mode)   # 캐시 사용(반복계산 제거)
            # 재무가 Yes 이고 (게이트가 꺼져있거나) 가격도 하락 중일 때만 매도
            if verdict in trigger and (not price_gate or is_falling(grid, s, d, gate_lookback)):
                later = trading_days[trading_days >= pd.Timestamp(d)]
                if len(later) > 0:
                    schedule[s] = later[0]          # 기준일 → 다음 거래일로 스냅
                break                               # 첫 트리거에서 매도하고 끝
            # Yes 지만 가격이 반등 중이면 매도하지 않고 다음 기준일을 계속 본다
    return schedule


#  세 전략 한 번에 

def run_three_strategies(grid, basket_codes, base_dir, ksic,
                         entry_date=DEFAULT_ENTRY_DATE, baseline_date=DEFAULT_BASELINE_DATE,
                         stop_pct=DEFAULT_STOP_PCT, trigger=DEFAULT_TRIGGER, end_date=None,
                         price_gate=True, with_sum=False):
    """한 바스켓(종목 목록)에 대해 전략별 자산가치 곡선 W_t(거래일별)를 돌려준다.
    모두 entry_date(2016-01-04)에 매수해 보유를 시작하고, 매도 규칙만 다르다.
    with_sum=True 면 합산 판정(서서히_합산) 전략을 하나 더 돌린다."""
    basket = grid[list(basket_codes)]
    schedules = {
        "무손절": {},
        "단순손절": stoploss_schedule(basket, stop_pct, start=entry_date, end_date=end_date),
        "서서히": seoseohi_schedule(base_dir, ksic, basket_codes, grid, baseline_date, trigger,
                                 end_date, price_gate, mode="count"),
    }
    if with_sum:
        schedules["서서히_합산"] = seoseohi_schedule(base_dir, ksic, basket_codes, grid, baseline_date,
                                                 trigger, end_date, price_gate, mode="sum")
    return {name: run_strategy(basket, sch, end_date=end_date) for name, sch in schedules.items()}, schedules


#  20종목 × 10회 무작위 바스켓 

def run_montecarlo(grid, base_dir, ksic=None, n_pick=20, n_runs=10, seed=0,
                   entry_date=DEFAULT_ENTRY_DATE, baseline_date=DEFAULT_BASELINE_DATE,
                   stop_pct=DEFAULT_STOP_PCT, trigger=DEFAULT_TRIGGER,
                   years=None, price_gate=False, with_sum=False):
    """120종목 중 n_pick 개를 무작위로 뽑아 n_runs 번 돌린다.
    한 회차 안에서는 세 전략이 '같은 바스켓'을 쓴다(전략 간 공정 비교).
    돌려주는 값: 회차별 최종수익률 표(long)와, 전략별 평균/승률 요약."""
    if years is None:
        end_date = None
    else:
        end_date = pd.Timestamp(entry_date) + pd.DateOffset(years=years)
    if ksic is None:
        ksic = load_ksic_from_base(base_dir)
    rng = np.random.default_rng(seed)
    all_codes = list(grid.columns)

    records = []
    for run_id in range(n_runs):
        basket_codes = list(rng.choice(all_codes, size=n_pick, replace=False))
        basket = grid[list(basket_codes)]
        curves, scheds = run_three_strategies(grid, basket_codes, base_dir, ksic,
                                              entry_date, baseline_date, stop_pct, trigger,
                                              end_date=end_date, price_gate=price_gate, with_sum=with_sum)
        for name, W in curves.items():
            rec = {"run": run_id, "strategy": name,
                   "누적수익률": cumret(W), "MDD": mdd(W),
                   "Sortino": sortino(W), "Sharpe": sharpe(W),
                   "final_W": W.iloc[-1]}
            # 현금대체효과: 매도가 있는 전략(단순손절·서서히)에만 의미가 있다
            ce = cash_effect_summary(basket, scheds.get(name, {}), end_date)
            rec.update({"매도수": ce["n_sells"], "좋은매도": ce["good"],
                        "성급한매도": ce["bad"], "평균_현금대체": ce["avg_B"]})
            records.append(rec)
    detail = pd.DataFrame(records)

    # 전략별 요약: 지표 평균 + '무손절 대비' 누적수익률 승률
    summary = detail.groupby("strategy").agg(
        누적수익률=("누적수익률", "mean"),
        수익률_중앙값=("누적수익률", "median"),
        MDD=("MDD", "mean"),
        Sortino=("Sortino", "mean"),
        Sharpe=("Sharpe", "mean"),
        평균_현금대체=("평균_현금대체", "mean"),
        평균_매도수=("매도수", "mean"),
    )
    pivot = detail.pivot(index="run", columns="strategy", values="누적수익률")
    if "무손절" in pivot.columns:
        for name in pivot.columns:
            summary.loc[name, "win_vs_무손절"] = (pivot[name] > pivot["무손절"]).mean()
    # 전략 순서를 보기 좋게 고정
    order = [s for s in ["무손절", "단순손절", "서서히", "서서히_합산"] if s in summary.index]
    return detail, summary.loc[order]


#  Phase 3 — 매도 사후분석: 판 종목이 '판 뒤에' 실제로 어떻게 됐나
# 서서히가 판 모든 종목에 대해, 안 팔고 끝까지 들고 있었으면 났을 수익률(R_hold)을 기록한다.
#   R_hold << 0  → 판 뒤 폭락 = '재앙 회피'(좋은 매도)
#   R_hold >> 0  → 판 뒤 급등 = '놓친 반등'(성급한 매도, 비싼 오판)
def sell_study(grid, base_dir, ksic=None, n_pick=20, n_runs=10, seed=0, years=4,
               trigger=DEFAULT_TRIGGER, baseline_date=DEFAULT_BASELINE_DATE,
               entry_date=DEFAULT_ENTRY_DATE, price_gate=False, mode="count"):
    if ksic is None:
        ksic = load_ksic_from_base(base_dir)
    end_date = None if years is None else pd.Timestamp(entry_date) + pd.DateOffset(years=years)
    rng = np.random.default_rng(seed)
    all_codes = list(grid.columns)

    rows = []
    for run_id in range(n_runs):
        basket_codes = list(rng.choice(all_codes, size=n_pick, replace=False))
        sched = seoseohi_schedule(base_dir, ksic, basket_codes, grid,
                                  baseline_date, trigger, end_date, price_gate, mode=mode)
        for stock, sell_date in sched.items():
            future = grid.loc[sell_date:, stock]
            if end_date is not None:
                future = future.loc[:pd.Timestamp(end_date)]
            future = future.dropna().iloc[1:]           # 판 다음날부터
            if len(future) == 0:
                continue
            R_hold = (1 + future / 100).prod() - 1       # 안 팔았으면 났을 수익률
            R_cash = (1 + CASH_DAILY_RATE) ** len(future) - 1
            rows.append({"run": run_id, "stock": stock, "sell_date": sell_date,
                         "R_hold": R_hold, "B": R_cash - R_hold, "good": (R_cash - R_hold) > 0})
    return pd.DataFrame(rows)


if __name__ == "__main__":
    from backtest.engine import load_daily_price
    import os
    from dotenv import load_dotenv
    load_dotenv()
    base = os.getenv("BACKTEST_DATA_DIR")
    grid = load_daily_price()
    detail, summary = run_montecarlo(grid, base, n_pick=20, n_runs=10, seed=0)
    print(summary.to_string())
