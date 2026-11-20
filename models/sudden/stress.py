"""stress.py — 임계·급락 모델 스트레스 테스트 (팀 backtest_01.ipynb의 run_stress와 같은 규칙)

[왜] 진입일 하나로 돌리면 그 직후 시장 사건에 결과가 좌우된다(예: 2020-01-02 진입 → 2020년 3월 코로나 바닥에서 임계 경보 78건).
     팀 서서히 백테스트처럼 진입일과 바스켓을 무작위로 바꿔 분포로 본다.

[규칙] (팀 run_stress와 같다)
  - 런마다 무작위 진입일(entry_min 이후, 진입+보유연수 ≤ 데이터 끝) × 120종목 중 무작위 20종목
  - 보유 끝 = 진입 + 보유연수. 엔진 run_strategy(basket, schedule, start_date=entry, end_date=end)
  - 지연(lag): 신호일을 lag 거래일 뒤로 민다. lag 0 = 신호일 종가, lag 1 = 다음 거래일 종가(현실적)
  - 무작위 매도: 전략과 같은 매도 수를 아무 종목·아무 날(진입~끝)에 판다. 3회 평균
  - 지표: 팀 backtest/metrics.py와 같은 정의(누적수익률, MDD = max(1 - W/고점), Sortino·Sharpe 무위험 0)

[전략]
  - 무손절 : 보유
  - 단순손절: 진입 대비 누적 -20% 첫날 (팀 stoploss_schedule과 같은 규칙)
  - 임계   : threshold.run_threshold(종목, 진입일, 끝, k, h)의 첫 경보일 (매수일 = 진입일에서 점수판 시작)
  - 급락   : 진입~끝 사이 종목별 첫 '가치훼손' 판정의 급락일 (판정 파일이 있을 때)
  - 임계_그리고_급락: 둘 다 나온 뒤(늦은 날)

    python models/sudden/stress.py --data-dir <팀 2020-2026 폴더> --threshold-prices models/sudden/cache/krx/prices_20181201_20261116.csv
    python models/sudden/stress.py ... --contract contracts/sudden_result_team120_2020_2026.json --horizons 1 2 3 4 --runs 150
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import events as ev
import joint_backtest as jb

HERE = Path(__file__).resolve().parent
OUT = HERE / 'cache/stress'
STOP_PCT = 0.20


# ---- 지표: 팀 backtest/metrics.py와 같은 정의 (models/sudden/backtest.py와 이름이 겹쳐 그 파일을 직접 import하지 않는다)
def cumret(W):
    return float(W.iloc[-1] / W.iloc[0] - 1)


def mdd(W):
    return float((1 - W / W.cummax()).max())


def sharpe(W):
    r = W.pct_change().dropna()
    sd = r.std()
    return float(r.mean() / sd * np.sqrt(252)) if sd and not np.isnan(sd) else np.nan


def sortino(W):
    r = W.pct_change().dropna()
    down = np.sqrt((r.clip(upper=0) ** 2).mean())
    return float(r.mean() / down * np.sqrt(252)) if down and not np.isnan(down) else np.nan


def cash_effect(basket, sched, end, cash_rate):
    """팀 metrics.cash_effect와 같은 정의: 판 종목마다 (매도 다음 날~끝 현금 이자) - (그 기간 보유 수익률).
    양수면 판 게 나았다. 매도가 없으면 NaN, 있으면 평균."""
    vals = []
    for s, d in sched.items():
        future = basket.loc[pd.Timestamp(d):, s].loc[:pd.Timestamp(end)].dropna().iloc[1:]
        if len(future):
            vals.append((1 + cash_rate) ** len(future) - 1 - ((1 + future / 100).prod() - 1))
    return float(np.mean(vals)) if vals else np.nan


# ---- 매도 일정
def shift_schedule(sched, days, lag):
    """매도일을 lag 거래일 뒤로 민다 (팀 shift_schedule과 같다)."""
    if lag == 0:
        return dict(sched)
    out = {}
    for s, d in sched.items():
        pos = min(days.searchsorted(pd.Timestamp(d)) + lag, len(days) - 1)
        out[s] = days[pos]
    return out


def snap(sched, days, end):
    """신호일을 거래일로 맞추고(그날이 휴장이면 다음 거래일) 보유 끝을 넘는 신호는 버린다."""
    out = {}
    for s, d in sched.items():
        pos = days.searchsorted(pd.Timestamp(d))
        if pos < len(days) and days[pos] <= end:
            out[s] = days[pos]
    return out


def stoploss_schedule(basket, entry, end, stop=STOP_PCT):
    """진입 대비 누적수익률이 -stop 이하가 된 첫 거래일."""
    w = basket[(basket.index >= entry) & (basket.index <= end)]
    cum = (1 + w.fillna(0) / 100).cumprod()
    return {s: cum.index[np.argmax((cum[s] <= 1 - stop).to_numpy())] for s in cum.columns if (cum[s] <= 1 - stop).any()}


def threshold_schedule(codes, entry, end, k, h):
    table = ev.T.run_threshold([(c, entry.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')) for c in codes], k=k, h=h)
    return {r.stock_code: r.first_alarm_date for r in table.itertuples() if pd.notna(r.first_alarm_date)}


def sudden_schedule(cases, codes, entry, end, verdicts=('가치훼손',)):
    """진입~끝 사이 종목별 첫 판정 급락일. cases = 판정 파일의 cases."""
    out = {}
    for c in sorted(cases, key=lambda c: c['drop_date']):
        d = pd.Timestamp(c['drop_date'])
        if c['stock_code'] in codes and c['verdict'] in verdicts and entry <= d <= end:
            out.setdefault(c['stock_code'], d)
    return out


def random_sell_schedule(codes, k, window, rng):
    """같은 매도 수를 아무 종목·아무 날에 (팀 random_sell_schedule과 같다)."""
    if k <= 0 or len(window) == 0:
        return {}
    picks = rng.choice(list(codes), size=min(k, len(codes)), replace=False)
    return {s: window[rng.integers(0, len(window))] for s in picks}


STRATEGY_ORDER = ('무손절', '단순손절', '임계', '급락', '임계_그리고_급락', '서서히', '서서히_합산', '서서히_그리고_급락', '합산_그리고_급락')


def add_slow(sched, slow_fn, codes, entry, end, days, lag):
    """서서히 신호(slow_fn → {'count': {...}, 'sum': {...}})와 급락과의 결합을 일정에 더한다. 결합은 둘 다 나온 뒤(늦은 날)."""
    if slow_fn is None:
        return
    sig = slow_fn(codes, entry, end)
    for name, mode in (('서서히', 'count'), ('서서히_합산', 'sum')):
        sched[name] = shift_schedule(snap(sig[mode], days, end), days, lag)
    if '급락' in sched:
        for name, base in (('서서히_그리고_급락', '서서히'), ('합산_그리고_급락', '서서히_합산')):
            sched[name] = {s: max(sched[base][s], sched['급락'][s]) for s in set(sched[base]) & set(sched['급락'])}


def run_stress(eng, grid, n_runs=150, n_pick=20, horizon_years=2, seed=7, lags=(0, 1), n_rand=3,
               entry_min='2020-01-02', k=ev.T.DEFAULT_K, h=ev.T.DEFAULT_H, cases=None, alarm_fn=None, slow_fn=None):
    """런별·전략별·lag별 한 줄. alarm_fn(codes, entry, end) → {종목: 경보일} (기본은 임계 모델)."""
    alarm_fn = alarm_fn or (lambda codes, entry, end: threshold_schedule(codes, entry, end, k, h))
    rng = np.random.default_rng(seed)                    # 진입일·바스켓 (전략·판정 파일과 상관없이 같은 런)
    rng_sell = np.random.default_rng(seed + 100_000)     # 무작위 매도. 따로 두어야 매도 수가 달라도 런 구성이 같다
    days = grid.index
    H = pd.DateOffset(years=horizon_years)
    elig = days[(days >= pd.Timestamp(entry_min)) & (days <= days[-1] - H)]
    codes_all = list(grid.columns)
    rows = []
    for run_id in range(n_runs):
        entry = elig[rng.integers(0, len(elig))]
        end = entry + H
        codes = list(rng.choice(codes_all, size=n_pick, replace=False))
        basket = grid[codes]
        win = days[(days >= entry) & (days <= end)]
        sched = {'무손절': {}, '단순손절': stoploss_schedule(basket, entry, end),
                 '임계': snap(alarm_fn(codes, entry, end), days, end)}
        if cases is not None:
            sched['급락'] = snap(sudden_schedule(cases, set(codes), entry, end), days, end)
            sched['임계_그리고_급락'] = {s: max(sched['임계'][s], sched['급락'][s]) for s in set(sched['임계']) & set(sched['급락'])}
        add_slow(sched, slow_fn, codes, entry, end, days, 0)
        for lag in lags:
            for name, sc in sched.items():
                sc_l = shift_schedule(sc, days, lag)
                W = eng.run_strategy(basket, sc_l, start_date=entry, end_date=end)
                rows.append({'run': run_id, 'entry': entry, 'horizon': horizon_years, 'strategy': name, 'lag': lag,
                             '누적수익률': cumret(W), 'MDD': mdd(W), 'Sortino': sortino(W), 'Sharpe': sharpe(W), '매도수': len(sc_l),
                             '현금대체': cash_effect(basket, sc_l, end, eng.CASH_DAILY_RATE)})
        for name in [s for s in sched if s not in ('무손절', '단순손절')]:
            n = len(sched[name])
            rr = [cumret(eng.run_strategy(basket, random_sell_schedule(codes, n, win, rng_sell), start_date=entry, end_date=end))
                  for _ in range(n_rand)]
            rows.append({'run': run_id, 'entry': entry, 'horizon': horizon_years, 'strategy': f'무작위매도_{name}', 'lag': 0,
                         '누적수익률': float(np.mean(rr)) if rr else np.nan, 'MDD': np.nan, 'Sortino': np.nan,
                         'Sharpe': np.nan, '매도수': n})
    return pd.DataFrame(rows)


def summary(detail, lag=1):
    """보유연수 × 전략별: 무손절 대비 중앙값과 승률, 같은 매도 수 무작위 매도 대비 승률. 런별로 짝지어 비교한다."""
    recs = []
    for hz, d in detail.groupby('horizon'):
        d0 = d[d['lag'] == lag]
        R = d0.pivot_table(index='run', columns='strategy', values='누적수익률')
        M = d0.pivot_table(index='run', columns='strategy', values='MDD')
        SH = d0.pivot_table(index='run', columns='strategy', values='Sharpe')
        rand = d[d['strategy'].str.startswith('무작위매도_')].pivot_table(index='run', columns='strategy', values='누적수익률')
        for s in [c for c in STRATEGY_ORDER[1:] if c in R]:
            rec = {'보유연수': hz, '전략': s, '런수': int(R[s].notna().sum()),
                   '매도수_중앙': float(d0[d0['strategy'] == s]['매도수'].median()),
                   '수익_무손절': R['무손절'].median(), '수익_전략': R[s].median(),
                   '수익개선_중앙': (R[s] - R['무손절']).median(),
                   'MDD_무손절': M['무손절'].median(), 'MDD_전략': M[s].median(),
                   'MDD개선_중앙': (M['무손절'] - M[s]).median(),
                   'Sharpe개선_중앙': (SH[s] - SH['무손절']).median(),
                   '현금대체_중앙': float(d0[d0['strategy'] == s]['현금대체'].median()),
                   '승률_vs무손절': float((R[s] > R['무손절']).mean())}
            if f'무작위매도_{s}' in rand:
                both = pd.concat([R[s], rand[f'무작위매도_{s}']], axis=1).dropna()
                rec['승률_vs무작위매도'] = float((both.iloc[:, 0] > both.iloc[:, 1]).mean())
                rec['우위_vs무작위매도_중앙'] = float((both.iloc[:, 0] - both.iloc[:, 1]).median())
            recs.append(rec)
    return pd.DataFrame(recs).round(3)


def run_montecarlo(eng, grid, entry_date, years, n_pick=20, n_runs=10, seed=0, lag=1, cases=None, alarm_fn=None,
                   k=ev.T.DEFAULT_K, h=ev.T.DEFAULT_H, slow_fn=None):
    """팀 backtest/montecarlo.run_montecarlo와 같은 방식: 진입일 고정, 120종목 중 20종목 바스켓 n_runs개(seed 0).
    바스켓 뽑는 순서가 팀과 같아 같은 seed면 같은 바스켓이다(무손절 값이 팀 표와 같아야 한다).
    임계·급락 신호는 lag 거래일 뒤에 판다(기본 1 = 다음 거래일). 단순손절은 팀과 같이 그날 판다."""
    alarm_fn = alarm_fn or (lambda codes, entry, end: threshold_schedule(codes, entry, end, k, h))
    entry = pd.Timestamp(entry_date)
    end = entry + pd.DateOffset(years=years)
    days = grid.index
    rng = np.random.default_rng(seed)
    all_codes = list(grid.columns)
    recs = []
    for run_id in range(n_runs):
        codes = list(rng.choice(all_codes, size=n_pick, replace=False))
        basket = grid[codes]
        sched = {'무손절': {}, '단순손절': stoploss_schedule(basket, entry, end),
                 '임계': shift_schedule(snap(alarm_fn(codes, entry, end), days, end), days, lag)}
        if cases is not None:
            sudden = shift_schedule(snap(sudden_schedule(cases, set(codes), entry, end), days, end), days, lag)
            sched['급락'] = sudden
            sched['임계_그리고_급락'] = {s: max(sched['임계'][s], sudden[s]) for s in set(sched['임계']) & set(sudden)}
        add_slow(sched, slow_fn, codes, entry, end, days, lag)
        for name, sc in sched.items():
            W = eng.run_strategy(basket, sc, start_date=entry, end_date=end)
            recs.append({'run': run_id, 'strategy': name, '누적수익률': cumret(W), 'MDD': mdd(W), 'Sortino': sortino(W),
                         'Sharpe': sharpe(W), '매도수': len(sc), '평균_현금대체': cash_effect(basket, sc, end, eng.CASH_DAILY_RATE)})
    detail = pd.DataFrame(recs)
    summ = detail.groupby('strategy').agg(누적수익률=('누적수익률', 'mean'), 수익률_중앙값=('누적수익률', 'median'),
                                          MDD=('MDD', 'mean'), Sortino=('Sortino', 'mean'), Sharpe=('Sharpe', 'mean'),
                                          평균_현금대체=('평균_현금대체', 'mean'), 평균_매도수=('매도수', 'mean'))
    piv = detail.pivot(index='run', columns='strategy', values='누적수익률')
    summ['win_vs_무손절'] = [(piv[s] > piv['무손절']).mean() for s in summ.index]
    order = [s for s in STRATEGY_ORDER if s in summ.index]
    return detail, summ.loc[order]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', type=Path, help='팀 BACKTEST_DATA_DIR 형식 폴더 (예: 120종목 2020-2026). 없으면 --legacy19')
    ap.add_argument('--threshold-prices', type=Path, help='임계 모델이 읽을 KRX 형식 시세 (--data-dir와 함께)')
    ap.add_argument('--legacy19', action='store_true', help='팀 19종목 2016~2019 (급락 판정이 있는 개발 표본)')
    ap.add_argument('--pick', type=int, default=20, help='바스켓 종목 수 (팀 120종목 기준 20, 19종목이면 10)')
    ap.add_argument('--contract', type=Path, help='급락 판정 파일 (없으면 급락 전략 생략)')
    ap.add_argument('--horizons', type=int, nargs='+', default=[1, 2, 3, 4])
    ap.add_argument('--runs', type=int, default=150)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--entry-min', default='2020-01-02')
    ap.add_argument('--k', type=float, default=ev.T.DEFAULT_K)
    ap.add_argument('--h', type=float, default=ev.T.DEFAULT_H)
    ap.add_argument('--out', type=Path, default=OUT)
    ap.add_argument('--seoseohi', action='store_true',
                    help='서서히 신호와 급락 결합도 본다(--legacy19 전용, 환경변수 SEOSEOHI_TTM_DIR·SEOSEOHI_FINAL_DATA_DIR 필요)')
    ap.add_argument('--montecarlo', action='store_true',
                    help='팀 run_montecarlo 방식(진입 고정, 바스켓 10개, seed 0): 2020 진입 2·4년 + 진입 연도별 2년(워크포워드)')
    args = ap.parse_args(argv)
    eng = jb.load_engine()
    if args.legacy19:                                    # 개발 표본: 팀 19종목 2016~2019 (임계는 기본 19종목 주가)
        grid = jb.load_legacy_basket()
    else:
        if not (args.data_dir and args.threshold_prices):
            ap.error('--data-dir와 --threshold-prices가 필요하다 (또는 --legacy19)')
        grid = jb.load_basket(eng, args.data_dir)
        ev.T.use_prices(args.threshold_prices, codes=list(grid.columns))
    cases = json.loads(args.contract.read_text(encoding='utf-8'))['cases'] if args.contract and args.contract.exists() else None
    slow_fn = None
    if args.seoseohi:
        import seoseohi_signal
        slow = seoseohi_signal.Seoseohi()
        slow_fn = lambda codes, entry, end: slow.first_signals(codes, entry, end)
    if args.montecarlo:
        pd.set_option('display.width', 250)
        out = {}
        pick = min(args.pick, len(grid.columns))
        if args.legacy19:   # 데이터 끝(2019-12-30)을 넘는 보유는 데이터 끝에서 멈춘다
            runs = [(f'전체 {len(grid.columns)}종목 · 2016-01-04 진입 · 끝까지', '2016-01-04', 4, len(grid.columns), 1),
                    (f'2016 진입 · 2년 · {pick}종목 바스켓 10개', '2016-01-04', 2, pick, 10),
                    (f'2016 진입 · 4년 · {pick}종목 바스켓 10개', '2016-01-04', 4, pick, 10)] + \
                   [(f'{yr} 진입 · 2년 (워크포워드)' + (' · 데이터 끝까지' if yr == 2018 else ''), f'{yr}-01-02', 2, pick, 10)
                    for yr in (2017, 2018)]
        else:
            runs = [(f'{args.entry_min[:4]} 진입 · {y}년', args.entry_min, y, pick, 10) for y in (2, 4)] + \
                   [(f'{yr} 진입 · 2년 (워크포워드)', f'{yr}-01-02', 2, pick, 10) for yr in range(2020, 2025)]
        for label, entry, years, n_pick, n_runs in runs:
            _, s = run_montecarlo(eng, grid, entry, years, n_pick=n_pick, n_runs=n_runs, seed=0, cases=cases, k=args.k, h=args.h, slow_fn=slow_fn)
            print(f'\n=== {label} ===\n{s.round(3).to_string()}', flush=True)
            out[label] = s.round(4).reset_index().to_dict(orient='records')
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / f'montecarlo_k{args.k:g}_h{args.h:g}.json').write_text(json.dumps(
            {'k': args.k, 'h': args.h, 'seed': 0, 'baskets': 10, 'lag': 1,
             'contract': str(args.contract) if cases is not None else None, 'tables': out}, ensure_ascii=False, indent=1), encoding='utf-8')
        return 0
    parts = []
    for hz in args.horizons:
        d = run_stress(eng, grid, n_runs=args.runs, n_pick=min(args.pick, len(grid.columns)), horizon_years=hz, seed=args.seed,
                       entry_min=args.entry_min,
                       k=args.k, h=args.h, cases=cases, slow_fn=slow_fn)
        parts.append(d)
        print(f'보유 {hz}년: {d["run"].nunique()}런', flush=True)
    detail = pd.concat(parts, ignore_index=True)
    args.out.mkdir(parents=True, exist_ok=True)
    tag = f'k{args.k:g}_h{args.h:g}_seed{args.seed}'
    detail.to_csv(args.out / f'stress_detail_{tag}.csv', index=False, encoding='utf-8-sig')
    res = {lag: summary(detail, lag) for lag in (0, 1)}
    (args.out / f'stress_summary_{tag}.json').write_text(json.dumps(
        {'k': args.k, 'h': args.h, 'seed': args.seed, 'runs': args.runs, 'contract': str(args.contract) if cases is not None else None,
         **{f'lag{lag}': t.to_dict(orient='records') for lag, t in res.items()}}, ensure_ascii=False, indent=1), encoding='utf-8')
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', None)
    for lag, t in res.items():
        print(f'\n[lag {lag}]' + (' 다음 거래일 매도(현실적)' if lag else ' 신호일 종가 매도'))
        print(t.to_string(index=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
