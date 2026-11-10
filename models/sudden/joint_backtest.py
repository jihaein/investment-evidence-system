"""서서히 모델과 급락 모델을 같은 엔진(backtest/engine.py run_strategy)으로 함께 백테스트한다.

[포트폴리오] 엔진 그대로: 19종목 동일 비중, 2016-01-04 시작, 수수료 0.1%, 현금 일 이자, 한 번 팔면 다시 사지 않는다.
[매도 신호] 신호가 나온 날 장 마감까지의 정보로 판단하므로 매도는 다음 거래일 종가로 한다(두 모델 같은 규칙).
  - 서서히: threshold.run_threshold(종목, 2016-01-04, 2019-12-30, k, h)의 첫 경보일
  - 급락: contracts/sudden_result.json의 판정 (export_sudden_contract.py)
  - 대조군: 비가치 판정에 매도, 판정과 상관없이 첫 급락에 매도. 급락 판정이 정보가 있다면 가치훼손 매도가 대조군보다 나아야 한다.
[지표] 누적수익률, 연 수익률, 연 변동성, 샤프·소르티노(무위험 = 엔진 현금 이자), 최대낙폭(MDD).
      매도마다 "피한 수익률" = 매도 다음 날부터 끝까지 그 종목의 누적 수익률 (음수면 매도가 도움이 됐다).
[주가] 엔진은 DAILY_PRICE_DATA_DIR의 05_주가_일별_2015-2019.csv를 읽는다. 설정이 없으면 data/ohlson을 쓴다(같은 파일).

    python models/sudden/joint_backtest.py                  # → models/sudden/cache/joint_backtest/
    python models/sudden/joint_backtest.py --k 0.5 --h 5
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

import events as ev
import export_sudden_contract as esc

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
START, END = '2016-01-04', '2019-12-30'
OUT = HERE / 'cache/joint_backtest'


def load_engine():
    """models/sudden/backtest.py와 이름이 겹쳐 파일 경로로 불러온다."""
    os.environ.setdefault('DAILY_PRICE_DATA_DIR', str(ROOT / 'data/ohlson'))
    spec = importlib.util.spec_from_file_location('team_engine', ROOT / 'backtest/engine.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def next_trading_day(dates, day):
    """day 다음 거래일 (Timestamp). 없으면 None."""
    later = dates[dates > pd.Timestamp(day)]
    return later[0] if len(later) else None


def to_schedule(signal_days, dates):
    """{종목: 신호일} → 엔진 입력 {종목: 매도일(다음 거래일)}"""
    out = {}
    for code, day in signal_days.items():
        d = next_trading_day(dates, day)
        if d is not None:
            out[code] = d
    return out


def gradual_signals(codes, k, h):
    table = ev.T.run_threshold([(c, START, END) for c in codes], k=k, h=h)
    return {r.stock_code: r.first_alarm_date for r in table.itertuples() if pd.notna(r.first_alarm_date)}


def combine(a, b, how):
    if how == 'or':                                     # 둘 중 먼저 울린 날
        return {c: min(d for d in (a.get(c), b.get(c)) if d is not None) for c in set(a) | set(b)}
    return {c: max(a[c], b[c]) for c in set(a) & set(b)}   # and: 둘 다 울린 뒤


def metrics(w, rf_daily):
    r = w.pct_change().dropna()
    ex = r - rf_daily
    years = len(r) / 252
    down = ex[ex < 0]
    peak = w.cummax()
    return {'final_W': round(float(w.iloc[-1])), 'cum_return': round(float(w.iloc[-1] / w.iloc[0] - 1), 4),
            'cagr': round(float((w.iloc[-1] / w.iloc[0]) ** (1 / years) - 1), 4),
            'ann_vol': round(float(r.std() * np.sqrt(252)), 4),
            'sharpe': round(float(ex.mean() / ex.std() * np.sqrt(252)), 3) if ex.std() else None,
            'sortino': round(float(ex.mean() / np.sqrt((down ** 2).mean()) * np.sqrt(252)), 3) if len(down) else None,
            'mdd': round(float((w / peak - 1).min()), 4)}


def avoided(basket, schedule, recovery_rate):
    """매도마다 매도 다음 날부터 끝까지 그 종목의 누적 수익률. 상장폐지 종목은 마지막 가격 × recovery_rate."""
    rows = []
    for code, day in sorted(schedule.items(), key=lambda x: x[1]):
        s = basket[code]
        after = s[s.index > day].dropna()
        cum = float((1 + after / 100).prod() - 1) if len(after) else 0.0
        delisted = s.last_valid_index() < basket.index[-1]
        if delisted:
            cum = (1 + cum) * recovery_rate - 1
        rows.append({'stock_code': code, 'sell_date': day.strftime('%Y-%m-%d'), 'after_return': round(cum, 4),
                     'delisted_later': bool(delisted)})
    return rows


def placebo(eng, basket, schedule, draws=200, seed=0):
    """우연 기준선. 이 표본은 19종목이 대체로 하락해 아무 때나 팔아도 좋아 보이므로 무작위와 비교한다.
    - random_stocks: 같은 매도일들을 무작위 종목에 배정 (종목 고르기 능력)
    - random_dates : 같은 종목을 무작위 거래일에 매도 (매도 시점 능력)
    값은 전략의 누적수익률이 무작위 결과 중 몇 %보다 높은지(백분위)와 무작위 중앙값."""
    if not schedule:
        return None
    rng = np.random.default_rng(seed)
    dates = basket.index[basket.index >= START]
    last = {c: basket[c].last_valid_index() for c in basket.columns}
    # 엔진은 상장폐지 뒤 날짜에 팔면 현금이 NaN이 된다. 상장폐지 종목은 그 전 날짜만 쓰고, 넘는 매도는 뺀다(이미 정리됨)
    alive = lambda sched: {c: d for c, d in sched.items() if d <= last[c]}
    real = float(eng.run_strategy(basket, schedule).iloc[-1])
    out = {}
    for kind in ('random_stocks', 'random_dates'):
        finals = []
        for _ in range(draws):
            if kind == 'random_stocks':
                picks = rng.choice(basket.columns, size=len(schedule), replace=False)
                sched = alive(dict(zip(picks, schedule.values())))
            else:
                sched = {}
                for c in schedule:
                    ok = dates[dates <= last[c]]
                    sched[c] = ok[rng.integers(len(ok))]
            finals.append(float(eng.run_strategy(basket, sched).iloc[-1]))
        finals = np.array(finals)
        assert not np.isnan(finals).any(), 'placebo run produced NaN'
        out[kind] = {'percentile': round(float((finals < real).mean()), 3),
                     'median_cum_return': round(float(np.median(finals) / eng.INITIAL_CASH - 1), 4)}
    return out


def run(k=ev.T.DEFAULT_K, h=ev.T.DEFAULT_H, contract_path=ROOT / 'contracts/sudden_result.json', draws=200):
    eng = load_engine()
    basket = eng.load_daily_price()
    dates = basket.index[basket.index >= START]
    codes = list(basket.columns)
    contract = json.loads(Path(contract_path).read_text(encoding='utf-8'))
    first = lambda verdicts, fragile=False: esc.sell_schedule(contract, verdicts, skip_fragile=fragile)
    signals = {
        'gradual': gradual_signals(codes, k, h),
        'sudden_value_impair': first(('가치훼손',)),
        'sudden_value_impair_robust': first(('가치훼손',), True),
        'control_sudden_non_value': first(('비가치',)),
        'control_any_drop': first(('가치훼손', '비가치', '판단보류')),
    }
    signals['gradual_or_sudden'] = combine(signals['gradual'], {c: pd.Timestamp(d) for c, d in signals['sudden_value_impair'].items()}, 'or')
    signals['gradual_and_sudden'] = combine(signals['gradual'], {c: pd.Timestamp(d) for c, d in signals['sudden_value_impair'].items()}, 'and')
    strategies = {'buy_and_hold': {}, **{name: to_schedule(s, dates) for name, s in signals.items()}}
    result, curves, sells = {}, {}, {}
    for rr in (1.0, 0.0):
        for name, sched in strategies.items():
            w = eng.run_strategy(basket, sched, recovery_rate=rr)
            key = name if rr == 1.0 else f'{name}@recovery0'
            result[key] = {**metrics(w, eng.CASH_DAILY_RATE), 'n_sold': len(sched)}
            if rr == 1.0:
                curves[name] = w
                av = avoided(basket, sched, rr)
                sells[name] = av
                if av:
                    vals = [a['after_return'] for a in av]
                    result[key].update({'sold_after_return_mean': round(float(np.mean(vals)), 4),
                                        'sold_after_return_median': round(float(np.median(vals)), 4),
                                        'share_sells_helped': round(float(np.mean([v < 0 for v in vals])), 3)})
                if draws:
                    result[key]['placebo'] = placebo(eng, basket, sched, draws)
    return {'k': k, 'h': h, 'start': START, 'end': str(dates[-1].date()), 'stocks': len(codes),
            'contract': {'rule_version': contract['rule_version'], 'sample': contract['sample'], 'params': contract['params']},
            'strategies': result, 'sells': sells}, pd.DataFrame(curves)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--k', type=float, default=ev.T.DEFAULT_K)
    ap.add_argument('--h', type=float, default=ev.T.DEFAULT_H)
    ap.add_argument('--out', type=Path, default=OUT)
    ap.add_argument('--draws', type=int, default=200, help='무작위 기준선 반복 수 (0이면 생략)')
    args = ap.parse_args(argv)
    res, curves = run(args.k, args.h, draws=args.draws)
    args.out.mkdir(parents=True, exist_ok=True)
    tag = f'k{args.k:g}_h{args.h:g}'
    (args.out / f'joint_backtest_{tag}.json').write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding='utf-8')
    curves.to_csv(args.out / f'equity_{tag}.csv', encoding='utf-8-sig')
    cols = ['n_sold', 'cum_return', 'cagr', 'ann_vol', 'sharpe', 'sortino', 'mdd', 'sold_after_return_median', 'share_sells_helped']
    table = pd.DataFrame(res['strategies']).T.reindex(columns=cols)
    print(table[[c for c in cols if not c.startswith('sold_')]].to_string())
    print('\n[무작위 기준선] 백분위(높을수록 무작위보다 좋음) / 무작위 중앙 누적수익률')
    for name, r in res['strategies'].items():
        p = r.get('placebo')
        if p and '@' not in name:
            print(f"{name:28s} 종목무작위 {p['random_stocks']['percentile']:.2f} ({p['random_stocks']['median_cum_return']:+.3f})"
                  f" | 시점무작위 {p['random_dates']['percentile']:.2f} ({p['random_dates']['median_cum_return']:+.3f})")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
