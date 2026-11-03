"""
threshold.py - 임계모델 1차 구현

[what]
매수일 이후 주가가 꾸준히 나빠지고 있는지 매 거래일을 확인해서 경보를 켜고 끔.
(ohlson, fscore, dupont은 왜 나빠졌나를 보여주는 증거이고, 이 모델은 언제 울릴지를 정함)

[order of calculation]
1. 하루 수익률 r_t: 거래소 등락률(FLUC_RT). 거래정지일(거래량 0)은 수익률 x
2. "평소" 평균  μ_t, 표준편차 σ_t: 평가일 t 기준으로 매일 새로 계산
    계산식: σ_t² = 0.2·σ²[t-90, t-30] + 0.8·σ²[t-150, t-91]   (구간 안 평균도 같은 비중, 최근 30일은 제외)
3. 점수판: z_t = (r_t - μ_t) / σ_t,  S_t = max(0, S_(t-1) - z_t - k)
    매수일에 S=0에서 시작해 평가일까지 계속 이어서 계산
4. 경보 = 조건1 and 조건2 (조건3은 설정으로 켜고 끈다)
    조건1: 누적 수익률 < 0 (기준일 = 매수일과 90거래일 전 중 더 가까운 날)
    조건2: S_t >= h
    조건3: 점수가 0에서 하루 만에 h 이상이 된 구간이 아닐 것(기본: 사용x, 기록은 남김)
5. 알림은 꺼져있다가 켜지는 날에만(alarm_onset)

[decisions]
- 평균 μ_t는 σ_t와 같은 두 구간/같은 비중(20%, 80%)으로 구함.
- 조건 1은 '매수일에서 120거래일까지 늘어나다가 이후 최근 120거래일'
- 조건 3(하루 폭락 거르기)은 쓰지 않고 기록만 남김(급락 모델과 겹침은 orchestra에서 정리)

[how to use]
from threshold import run_threshold
table = run_threshold([("010170", "2016-06-30", "2019-06-28)]), k=0.5, h=5)
table = run_threshold(pairs, k=0.5, h=5, baseline="fixed") #기준 고정 방식으로 비교

[execution]
python threshold.py -> 데이터 점검 + 경보 빈도(k * h) + 알려진 사례
"""

import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd


HERE = Path(__file__).resolve().parent                        # models/seoseohi
PROJECT_ROOT = HERE.parents[1]
PRICE_PATH = PROJECT_ROOT / "data" / "ohlson" / "05_주가_일별_2015-2019.csv"
OUTPUT_DIR = HERE / "output"

K_LIST = [0.1, 0.25, 0.5]               # 백테스트에서 비교할 k
H_LIST = [2, 3, 5]                      # 백테스트에서 비교할 h
DEFAULT_K, DEFAULT_H = 0.5, 5

EXTENDED_K = [0.75, 1.0]                # --extended 로 실행할 때만 더 넣는 k (h·k를 높이면 경보가 얼마나 줄어드는지 보려는 용도)
EXTENDED_H = [8, 10]                    # --extended 로 실행할 때만 더 넣는 h

COND1_DAYS = 120                        # 조건 1 관찰기간(거래일). 팀 결정: 90 → 120
RECENT_WINDOW = (30, 90)                # t-90 ~ t-30 거래일 (반영 비중 20%)
OLD_WINDOW = (91, 150)                  # t-150 ~ t-91 거래일 (반영 비중 80%)
RECENT_WEIGHT, OLD_WEIGHT = 0.2, 0.8
BASELINE_FORM = "weighted"              # "weighted": 위 두 구간을 20%·80%로 섞음(팀 식) / "single": 아래 한 구간만 사용
SINGLE_WINDOW = (31, 150)               # single: t-150 ~ t-31 거래일 (120거래일, 최근 30일 제외)
MIN_OBS = 30                            # 구간 안 수익률이 이 개수보다 적으면 "평소"를 못 구함
SIGMA_FLOOR = 0.001                     # 표준편차 하한(0.1%). 거의 안 움직인 종목의 z 폭주 방지
COND3_MODE = "off"                      # 조건 3(하루 폭락 거르기): "off" 사용 안 함(팀 결정) / "share" 구간 안 하루 비중이 아래 값 이상이면 제외 / "literal" 0에서 하루 만에 h 도달한 구간 제외
COND3_SHARE_LIMIT = 0.5                 # share: 구간에 쌓인 점수의 이 비율 이상을 하루가 만들었으면 서서히 하락이 아니라고 본다 (임의값, 백테스트로 확인)
BASELINE_MODE = "moving"                # "moving": 평소를 평가일마다 새로 계산(팀 결정) / "fixed": 매수일에 정한 값으로 고정(비교용)
HIT_HORIZON_DAYS = 60                   # 적중 판정: 경보가 켜진 날 이후 이 거래일 안에
HIT_DROP = -0.20                        # 종가가 이만큼(-20%) 이상 내려가면 맞음
MAX_GAP_DAYS = 7                        # 매수일·평가일이 휴장일이면 직전 거래일을 찾는 최대 일수
RET_GAP_WARN = 0.005                    # 등락률과 종가 수익률이 이만큼 다르면 보정 의심일로 표시



# 1. 주가 읽기

def to_number(series):
    return pd.to_numeric(series.astype(str).str.replace(",", "").str.strip(), errors="coerce")


def load_prices(path=PRICE_PATH):
    raw = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    raw.columns = [c.strip() for c in raw.columns]
    raw = raw[raw["BAS_DD"].str.fullmatch(r"\d{8}", na=False)]      
    prices = pd.DataFrame({
        "stock_code": raw["ISU_CD"].str.strip(),
        "company_name": raw["ISU_NM"].str.strip(),
        "trade_date": pd.to_datetime(raw["BAS_DD"], format="%Y%m%d"),
        "close": to_number(raw["TDD_CLSPRC"]),
        "ret_krx": to_number(raw["FLUC_RT"]) / 100,                  # 등락률(%) → 비율
        "volume": to_number(raw["ACC_TRDVOL"]),
    })
    return prices.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)



# 2. 종목별 수익률·"평소" 계산 (매수일과 상관없이 날짜마다 정해진다)

def window_stats(r, window):
    lo, hi = window
    length = hi - lo + 1                                              # 예: 30~90 → 61개
    roll = r.rolling(length, min_periods=MIN_OBS)
    return roll.mean().shift(lo), roll.var().shift(lo)                # t 기준 [t-hi, t-lo] 구간


def prepare_stock(g, form=None):
    form = form or BASELINE_FORM
    g = g.sort_values("trade_date").reset_index(drop=True).copy()
    g["ret_close"] = g["close"].pct_change()
    g["halted"] = g["volume"].fillna(0) == 0                          # 거래량 0 = 거래정지
    g["ret"] = g["ret_krx"].where(~g["halted"])                       # 거래정지일은 수익률 없음
    g["ret_gap"] = (g["ret_krx"] - g["ret_close"]).abs().where(~g["halted"])

    if form == "single":                                              # 120거래일 한 구간, 최근 30일 제외
        g["mu"], var = window_stats(g["ret"], SINGLE_WINDOW)
        g["sigma"] = np.sqrt(var).clip(lower=SIGMA_FLOOR)
        first_ok = SINGLE_WINDOW[1]
    else:                                                             # 두 구간을 20%·80%로 섞음
        m_recent, v_recent = window_stats(g["ret"], RECENT_WINDOW)
        m_old, v_old = window_stats(g["ret"], OLD_WINDOW)
        g["mu"] = RECENT_WEIGHT * m_recent + OLD_WEIGHT * m_old
        g["sigma"] = np.sqrt(RECENT_WEIGHT * v_recent + OLD_WEIGHT * v_old).clip(lower=SIGMA_FLOOR)
        first_ok = OLD_WINDOW[1]
    too_early = g.index < first_ok                                    # 150거래일 자료가 다 쌓이기 전
    g.loc[too_early, ["mu", "sigma"]] = np.nan
    g["cum_index"] = (1 + g["ret"].fillna(0)).cumprod()               # 누적 수익률 계산용 (정지일은 0%)
    return g


@lru_cache(maxsize=2)
def _prepared(form):
    prices = load_prices()
    return {code: prepare_stock(g, form) for code, g in prices.groupby("stock_code", sort=False)}


def prepared_prices():
    return _prepared(BASELINE_FORM)                                   # BASELINE_FORM을 바꾸면 새로 계산한다



# 3. 점수판 계산: 매수일에 0에서 시작해 이어서 계산

def run_cusum(g, buy_idx, end_idx, k, h, baseline=None, cond3=None):
    n = len(g)
    ret, mu, sigma = g["ret"].to_numpy(), g["mu"].to_numpy(), g["sigma"].to_numpy()
    if (baseline or BASELINE_MODE) == "fixed":                        # 매수일 기준 평균·표준편차를 계속 쓴다
        mu, sigma = np.full(n, mu[buy_idx]), np.full(n, sigma[buy_idx])
    cum = g["cum_index"].to_numpy()

    z = np.full(n, np.nan)
    S = np.zeros(n)
    day_hold = np.zeros(n, bool)                  # 오늘은 점수판을 갱신하지 못한 날 (정지일·평소 없음)
    run_begin = np.full(n, -1)
    max_share = np.full(n, np.nan)
    jump_run = np.zeros(n, bool)

    s_prev, begin, run_sum, run_max, run_jump = 0.0, -1, 0.0, 0.0, False
    for i in range(buy_idx + 1, end_idx + 1):
        if np.isnan(ret[i]) or np.isnan(mu[i]) or np.isnan(sigma[i]):
            day_hold[i] = True                    # 갱신하지 않고 어제 점수를 이어 간다
        else:
            z[i] = (ret[i] - mu[i]) / sigma[i]
            inc = -z[i] - k                       # 평소보다 떨어진 만큼(+) 오르고, 괜찮으면 깎인다
            if s_prev == 0.0 and inc > 0:         # 새 구간 시작
                begin, run_sum, run_max, run_jump = i, 0.0, 0.0, inc >= h
            if inc > 0:
                run_sum += inc
                run_max = max(run_max, inc)
            s_prev = max(0.0, s_prev + inc)
            if s_prev == 0.0:                     # 0으로 돌아가면 구간 종료
                begin, run_sum, run_max, run_jump = -1, 0.0, 0.0, False
        S[i] = s_prev
        run_begin[i] = begin
        jump_run[i] = run_jump
        if run_sum > 0:
            max_share[i] = run_max / run_sum      # 구간 안에서 하루가 차지한 비중

    # 조건 1: 기준일(매수일과 120거래일 전 중 더 가까운 날)부터 누적 수익률
    idx = np.arange(n)
    start = np.maximum(buy_idx, idx - COND1_DAYS)
    cum_ret = cum / cum[start] - 1

    live = (idx > buy_idx) & (idx <= end_idx)
    cond1 = live & (cum_ret < 0)
    cond2 = live & (S >= h)                                          # 정지일에도 이어진 점수로 판단
    mode = cond3 or COND3_MODE
    if mode == "share":                                              # 구간 안에서 하루가 차지한 비중이 한도 이상이면 제외
        excluded = np.nan_to_num(max_share, nan=0.0) >= COND3_SHARE_LIMIT
    elif mode == "literal":                                          # 0에서 하루 만에 h 이상이 된 구간 제외
        excluded = jump_run
    else:
        excluded = np.zeros(n, bool)
    alarm_raw = cond1 & cond2                                        # 조건 3을 적용하기 전 경보
    alarm = alarm_raw & ~excluded
    prev = np.concatenate([[False], alarm[:-1]])
    onset = alarm & ~prev                         # 꺼짐 → 켜짐인 날

    return {"z": z, "S": S, "day_hold": day_hold, "run_begin": run_begin, "max_share": max_share,
            "jump_run": jump_run, "cum_ret": cum_ret, "cond1": cond1, "cond2": cond2,
            "alarm": alarm, "alarm_raw": alarm_raw, "cond3_excluded": alarm_raw & excluded,
            "onset": onset, "live": live}


def cusum_frame(g, buy_idx, end_idx, k, h, baseline=None, cond3=None):
    res = run_cusum(g, buy_idx, end_idx, k, h, baseline, cond3)
    sl = slice(buy_idx, end_idx + 1)
    frame = g.iloc[sl][["stock_code", "trade_date", "ret", "mu", "sigma", "halted"]].copy()
    dates = g["trade_date"].to_numpy()
    frame["z"] = res["z"][sl]
    frame["cusum"] = res["S"][sl]
    frame["cum_ret"] = res["cum_ret"][sl]
    frame["cond1"] = res["cond1"][sl]
    frame["cond2"] = res["cond2"][sl]
    frame["run_start_date"] = [dates[b] if b >= 0 else np.datetime64("NaT", "ns") for b in res["run_begin"][sl]]
    frame["max_day_share"] = res["max_share"][sl]
    frame["one_day_jump_run"] = res["jump_run"][sl]
    frame["cond3_excluded"] = res["cond3_excluded"][sl]
    frame["alarm"] = res["alarm"][sl]
    frame["alarm_onset"] = res["onset"][sl]
    frame["hold_day"] = res["day_hold"][sl]
    return frame.reset_index(drop=True)



# 4. 매수일·평가일 쌍 평가

def locate(g, date, max_gap_days=MAX_GAP_DAYS):
    d = pd.Timestamp(date)
    idx = int(g["trade_date"].searchsorted(d, side="right")) - 1       # d 이전(포함) 마지막 거래일
    if idx < 0 or (d - g["trade_date"].iloc[idx]).days > max_gap_days:
        return None
    return idx


def evaluate_pair(by_stock, stock_code, buy_date, eval_date, k, h, baseline=None, cond3=None):
    out = {"stock_code": stock_code, "buy_date": buy_date, "eval_date": eval_date, "k": k, "h": h,
           "alarm_on": pd.NA, "hold_reason": ""}
    g = by_stock.get(stock_code)
    if g is None:
        out["hold_reason"] = "no_price_data"
        return out
    b, e = locate(g, buy_date), locate(g, eval_date)
    if b is None:
        out["hold_reason"] = "buy_no_data"
    elif e is None:
        out["hold_reason"] = "eval_no_data"            # 상장폐지 이후이거나 데이터가 없는 날
    elif e <= b:
        out["hold_reason"] = "eval_not_after_buy"
    elif (baseline or BASELINE_MODE) == "fixed" and np.isnan(g["sigma"].iloc[b]):
        out["hold_reason"] = "no_baseline_at_buy"       # 매수일에 150거래일 자료가 없어 기준을 못 정함
    elif np.isnan(g["sigma"].iloc[e]):
        out["hold_reason"] = "no_baseline_yet"          # 150거래일 자료가 아직 안 쌓임
    if out["hold_reason"]:
        return out

    res = run_cusum(g, b, e, k, h, baseline, cond3)
    live = res["live"]
    first = np.flatnonzero(res["alarm"] & live)
    out.update({
        "alarm_on": bool(res["alarm"][e]),
        "cusum_eval": float(res["S"][e]),
        "cum_ret_eval": float(res["cum_ret"][e]),
        "first_alarm_date": g["trade_date"].iloc[first[0]] if len(first) else pd.NaT,
        "days_to_first_alarm": int(first[0] - b) if len(first) else pd.NA,
        "n_onsets": int(res["onset"][live].sum()),
        "alarm_days": int(res["alarm"][live].sum()),
        "alarm_days_before_cond3": int(res["alarm_raw"][live].sum()),     # 조건 3을 적용하기 전 경보 일수
        "cond3_excluded_days": int(res["cond3_excluded"][live].sum()),     # 조건 3이 걸러낸 일수
        "first_alarm_day_share": float(res["max_share"][first[0]]) if len(first) else np.nan,   # 첫 경보 날 하루 비중 (높으면 급락)
        "max_cusum": float(res["S"][live].max()),
        "held_days": int(e - b),
        "halted_eval": bool(g["halted"].iloc[e]),
    })
    return out


def run_threshold(pairs, k=DEFAULT_K, h=DEFAULT_H, baseline=None, cond3=None):
    by_stock = prepared_prices()
    rows = [evaluate_pair(by_stock, code, buy, ev, k, h, baseline, cond3) for code, buy, ev in pairs]
    table = pd.DataFrame(rows)
    table.insert(0, "model", "threshold")
    table["interpretable"] = table["alarm_on"].notna()
    table["scale_status"] = "gate"                      # 점수가 아니라 켜짐/꺼짐
    table["baseline_mode"] = baseline or BASELINE_MODE
    table["cond3_mode"] = cond3 or COND3_MODE
    return table



# 5. 경보 빈도 확인 (k × h): 아무 일 없어도 얼마나 자주 울리는가

# 각 날의 종가 기준, 이후 horizon거래일 안의 가장 낮은 누적 수익률 (뒤에 자료가 모자란 날은 비움)
def forward_min_return(g, horizon=HIT_HORIZON_DAYS):
    cum = g["cum_index"].to_numpy()
    n = len(cum)
    fm = np.full(n, np.inf)
    for j in range(1, horizon + 1):
        shifted = np.full(n, np.nan)
        shifted[:n - j] = cum[j:]
        fm = np.fmin(fm, shifted / cum - 1)
    fm[max(n - horizon, 0):] = np.nan
    fm[np.isinf(fm)] = np.nan
    return fm


# 아무 날이나 골랐을 때 이후 horizon거래일 안에 HIT_DROP 이하로 내려간 비율 ("평소" 기준선)
def base_hit_rate(by_stock, fm_by):
    hits = scored = 0
    for code, g in by_stock.items():
        fm = fm_by[code]
        ok = g["sigma"].notna().to_numpy() & ~np.isnan(fm)
        hits += int((fm[ok] <= HIT_DROP).sum())
        scored += int(ok.sum())
    return hits / scored if scored else np.nan


def frequency_table(by_stock, k_list=K_LIST, h_list=H_LIST, horizons=(250, 500), step=60, baseline=None, cond3=None):
    fm_by = {code: forward_min_return(g) for code, g in by_stock.items()}
    base_rate = base_hit_rate(by_stock, fm_by)
    rows = []
    for k in k_list:
        for h in h_list:
            for horizon in horizons:
                n_hold, with_alarm, first_days, onsets = 0, 0, [], []
                onset_days = {code: set() for code in by_stock}          # 겹치는 보유 구간에서 같은 경보를 두 번 세지 않는다
                for code, g in by_stock.items():
                    ready = g["sigma"].notna().to_numpy()
                    if not ready.any():
                        continue
                    first_ready = int(np.argmax(ready))
                    for b in range(first_ready, len(g) - horizon, step):
                        res = run_cusum(g, b, b + horizon, k, h, baseline, cond3)
                        live = res["live"]
                        hits = np.flatnonzero(res["alarm"] & live)
                        n_hold += 1
                        with_alarm += len(hits) > 0
                        if len(hits):
                            first_days.append(hits[0] - b)
                        onsets.append(res["onset"][live].sum())
                        onset_days[code].update(np.flatnonzero(res["onset"] & live).tolist())
                scored = n_hit = 0
                for code, days in onset_days.items():
                    if days:
                        v = fm_by[code][np.fromiter(days, dtype=int)]
                        v = v[~np.isnan(v)]
                        scored += len(v)
                        n_hit += int((v <= HIT_DROP).sum())
                hit_rate = n_hit / scored if scored else np.nan
                rows.append({"baseline": baseline or BASELINE_MODE, "cond3": cond3 or COND3_MODE, "k": k, "h": h,
                             "horizon_days": horizon, "holds": n_hold,
                             "share_with_alarm": with_alarm / n_hold if n_hold else np.nan,
                             "median_days_to_first_alarm": float(np.median(first_days)) if first_days else np.nan,
                             "avg_onsets_per_hold": float(np.mean(onsets)) if onsets else np.nan,
                             "onsets_scored": scored, "hit_rate": hit_rate, "base_rate": base_rate,
                             "lift": hit_rate / base_rate if scored and base_rate else np.nan})
    return pd.DataFrame(rows)



# 6. 실행: 데이터 점검 → 경보 빈도 → 알려진 사례

EXAMPLE_PAIRS = [
    ("053660", "2016-06-30", "2019-06-28"),    # 현진소재: F-Score 7 → 1로 서서히 악화
    ("010170", "2016-06-30", "2019-06-28"),    # 대한광통신: 2018년 급락
    ("036890", "2016-06-30", "2019-06-28"),    # 진성티이씨
]

if __name__ == "__main__":
    by_stock = prepared_prices()
    all_days = pd.concat(by_stock.values(), ignore_index=True)

    print(f"[설정] 조건 1 관찰기간 {COND1_DAYS}거래일 / 평소 계산 방식 {BASELINE_FORM} / 평소 기준 {BASELINE_MODE}")
    print("[주가 데이터]")
    print("행 수:", len(all_days), "/ 종목 수:", len(by_stock),
          "/ 기간:", all_days["trade_date"].min().date(), "~", all_days["trade_date"].max().date())
    print("거래정지일(거래량 0):", int(all_days["halted"].sum()))
    print("등락률 결측:", int(all_days["ret_krx"].isna().sum()))
    gap = all_days[all_days["ret_gap"] > RET_GAP_WARN]
    print(f"등락률과 종가 수익률이 {RET_GAP_WARN:.1%}p 넘게 다른 날: {len(gap)}  (액면분할·증자·감자 등 보정일 의심)")
    if len(gap):
        print(gap[["company_name", "trade_date", "ret_krx", "ret_close"]].head(15).to_string(index=False))
    first_ready = {c: g.loc[g["sigma"].notna(), "trade_date"].min() for c, g in by_stock.items()}
    print("점수판 시작 가능 첫 날짜:", min(first_ready.values()).date(), "~", max(first_ready.values()).date())

    print()
    print("[경보 빈도] 매수 후 아무 때나 시작해 보유 1년·2년 동안 경보가 한 번이라도 켜진 비율")
    print("  moving = 평소를 매일 새로 계산(팀 결정), fixed = 매수일에 정한 값으로 고정(비교용)")
    extended = "--extended" in sys.argv
    k_list = sorted(set(K_LIST + (EXTENDED_K if extended else [])))
    h_list = sorted(set(H_LIST + (EXTENDED_H if extended else [])))
    if extended:
        print(f"  (확장 실행: k {k_list}, h {h_list})")
    freq_parts = [frequency_table(by_stock, k_list, h_list, baseline="moving"),
                  frequency_table(by_stock, k_list, h_list, baseline="fixed")]
    if extended:                                                     # 조건 3(하루 비중 50%)을 켠 결과도 같이 본다
        freq_parts.append(frequency_table(by_stock, k_list, h_list, baseline="moving", cond3="share"))
    freq = pd.concat(freq_parts, ignore_index=True)
    show = freq.drop(columns=["holds"]).rename(columns={"share_with_alarm": "alarm_share", "median_days_to_first_alarm": "first_days",
                                                         "avg_onsets_per_hold": "onsets", "horizon_days": "hold"})
    print(f"  (적중 = 경보가 켜진 날 이후 {HIT_HORIZON_DAYS}거래일 안에 {HIT_DROP:.0%} 이하. hit_rate = 적중 비율, base_rate = 아무 날이나 골랐을 때 비율, lift = hit_rate / base_rate)")
    print(show.round(3).to_string(index=False))

    print()
    print("[알려진 사례] 2016-06-30 매수 → 2019-06-28 평가")
    if extended:                                   # 사례 표가 길어지지 않게 이동 방식만, k 0.1·0.25·0.5 × h 5·8·10을 조건 3 끔/켬으로 비교
        ex = pd.concat([run_threshold(EXAMPLE_PAIRS, k, h, "moving", c3)
                        for c3 in ("off", "share") for k in (0.1, 0.25, 0.5) for h in (5, 8, 10)], ignore_index=True)
    else:
        ex = pd.concat([run_threshold(EXAMPLE_PAIRS, k, h, mode) for mode in ("moving", "fixed")
                        for k in k_list for h in h_list], ignore_index=True)
    ex = ex.sort_values(["stock_code", "baseline_mode", "k", "h", "cond3_mode"]).reset_index(drop=True)
    cols = ["stock_code", "baseline_mode", "cond3_mode", "k", "h", "alarm_on", "first_alarm_date", "days_to_first_alarm",
            "first_alarm_day_share", "n_onsets", "alarm_days", "cond3_excluded_days", "max_cusum", "hold_reason"]
    print(ex[[c for c in cols if c in ex.columns]].to_string(index=False))

    OUTPUT_DIR.mkdir(exist_ok=True)
    suffix = "_extended" if extended else ""                         # 기본 실행 결과 파일을 덮어쓰지 않는다
    freq.to_csv(OUTPUT_DIR / f"threshold_frequency{suffix}.csv", index=False, encoding="utf-8-sig")
    ex.to_csv(OUTPUT_DIR / f"threshold_examples{suffix}.csv", index=False, encoding="utf-8-sig")
    if "053660" in by_stock:                              # 현진소재 일별 점수판 
        g = by_stock["053660"]
        cusum_frame(g, locate(g, "2016-06-30"), locate(g, "2019-06-28"), DEFAULT_K, 3).to_csv(
            OUTPUT_DIR / "threshold_daily_053660.csv", index=False, encoding="utf-8-sig")
    print("\n저장 완료:", OUTPUT_DIR)