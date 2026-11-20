"""
joint_backtest.py

서서히 모델과 급락 모델을 같은 엔진(backtest/engine.py run_strategy)으로 함께 백테스트한다.

[포트폴리오]
엔진 그대로: 19종목 동일 비중, 
2016-01-04 시작, 
수수료 0.1%, 
현금 일 이자, 
한 번 팔면 다시 사지 않는다.

[매도 신호]
신호가 나온 날 장 마감까지의 정보로 판단하므로 매도는 다음 거래일 종가로 한다(두 모델 같은 규칙)

  - 서서히: threshold.run_threshold(종목, 2016-01-04, 2019-12-30, k, h)의 첫 경보일
  - 급락: contracts/sudden_result.json의 판정 (export_sudden_contract.py)
  - 대조군: 비가치 판정에 매도, 판정과 상관없이 첫 급락에 매도. 급락 판정이 정보가 있다면 가치훼손 매도가 대조군보다 나아야 함

[지표]
누적수익률, 연 수익률, 연 변동성, 샤프·소르티노(무위험 = 엔진 현금 이자), 최대낙폭(MDD).
팀 지표(backtest/metrics.py)와 같은 정의의 샤프·소르티노(무위험 0)도 함께 낸다(sharpe_rf0, sortino_rf0).
매도마다 "피한 수익률" = 매도 다음 날부터 끝까지 그 종목의 누적 수익률 (음수면 매도가 도움이 됐다)

[주가]
기본은 팀 19종목 표(DAILY_PRICE_DATA_DIR 또는 data/ohlson의 05_주가_일별_2015-2019.csv)를 이 파일에서 읽는다.
main의 엔진(2026-11-17)은 BACKTEST_DATA_DIR(120종목, 2016~2020·2020~2026) 형식만 읽으므로,
그 데이터로 돌릴 때는 --data-dir로 폴더를 준다. 급락 판정·임계 경보는 아직 19종목에만 있어 다른 종목은 신호가 없다.

    python models/sudden/joint_backtest.py                  # → models/sudden/cache/joint_backtest/
    python models/sudden/joint_backtest.py --k 0.5 --h 5
    python models/sudden/joint_backtest.py --data-dir <BACKTEST_DATA_DIR>
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
    spec = importlib.util.spec_from_file_location('team_engine', ROOT / 'backtest/engine.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_legacy_basket(path=None):
    """팀 19종목 2015~2019 일별 등락률(%) 표 (날짜 × 종목코드).
    2026-11-17 main의 엔진은 BACKTEST_DATA_DIR(120종목) 형식만 읽는다. 급락 판정은 아직 이 19종목에만 있어서
    예전 엔진의 읽기 방식(둘째 줄 한글 머리글 건너뜀, ISU_CD, FLUC_RT)을 여기에 그대로 둔다."""
    path = Path(path or Path(os.getenv('DAILY_PRICE_DATA_DIR', ROOT / 'data/ohlson')) / '05_주가_일별_2015-2019.csv')
    data = pd.read_csv(path, header=0, skiprows=[1], dtype=str, encoding='utf-8-sig', usecols=['BAS_DD', 'ISU_CD', 'FLUC_RT'])
    data['FLUC_RT'] = pd.to_numeric(data['FLUC_RT'], errors='coerce')
    data['BAS_DD'] = pd.to_datetime(data['BAS_DD'], errors='coerce')
    data['ISU_CD'] = data['ISU_CD'].str.zfill(6)
    return data.pivot(index='BAS_DD', columns='ISU_CD', values='FLUC_RT')


def load_basket(eng, data_dir=None):
    """data_dir(팀 BACKTEST_DATA_DIR 형식: <dir>/추가자료/시장자료/*.csv)가 있으면 main 엔진으로 읽고,
    없으면 19종목 표를 읽는다."""
    return eng.load_daily_price(str(data_dir)) if data_dir else load_legacy_basket()


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


def gradual_signals(codes, k, h, start=START, end=END):
    table = ev.T.run_threshold([(c, start, end) for c in codes], k=k, h=h)
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
    raw_down = np.sqrt((r.clip(upper=0) ** 2).mean())    # 팀 metrics.py 정의(무위험 0)
    team = {'sharpe_rf0': round(float(r.mean() / r.std() * np.sqrt(252)), 3) if r.std() else None,
            'sortino_rf0': round(float(r.mean() / raw_down * np.sqrt(252)), 3) if raw_down else None}
    return {**team, 'final_W': round(float(w.iloc[-1])), 'cum_return': round(float(w.iloc[-1] / w.iloc[0] - 1), 4),
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


def placebo(eng, basket, schedule, draws=200, seed=0, start=START, end=None):
    """우연 기준선. 이 표본은 19종목이 대체로 하락해 아무 때나 팔아도 좋아 보이므로 무작위와 비교한다.
    - random_stocks: 같은 매도일들을 무작위 종목에 배정 (종목 고르기 능력)
    - random_dates : 같은 종목을 무작위 거래일에 매도 (매도 시점 능력)
    값은 전략의 누적수익률이 무작위 결과 중 몇 %보다 높은지(백분위)와 무작위 중앙값."""
    if not schedule:
        return None
    rng = np.random.default_rng(seed)
    end = end or basket.index[-1]
    dates = basket.index[(basket.index >= start) & (basket.index <= end)]
    last = {c: basket[c].last_valid_index() for c in basket.columns}
    # 엔진은 상장폐지 뒤 날짜에 팔면 현금이 NaN이 된다. 상장폐지 종목은 그 전 날짜만 쓰고, 넘는 매도는 뺀다(이미 정리됨)
    alive = lambda sched: {c: d for c, d in sched.items() if d <= last[c]}
    real = float(eng.run_strategy(basket, schedule, start_date=start, end_date=end).iloc[-1])
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
            finals.append(float(eng.run_strategy(basket, sched, start_date=start, end_date=end).iloc[-1]))
        finals = np.array(finals)
        assert not np.isnan(finals).any(), 'placebo run produced NaN'
        out[kind] = {'percentile': round(float((finals < real).mean()), 3),
                     'median_cum_return': round(float(np.median(finals) / eng.INITIAL_CASH - 1), 4)}
    return out


def run(k=ev.T.DEFAULT_K, h=ev.T.DEFAULT_H, contract_path=ROOT / 'contracts/sudden_result.json', draws=200, data_dir=None,
        start=START, end=None, threshold_prices=None):
    """data_dir·threshold_prices를 주면 다른 종목·기간(예: 팀 120종목 2020~2026)으로 돌린다.
    threshold_prices: 임계 모델이 읽을 KRX 형식 시세(등락률·거래량 포함). 판정 파일이 없으면 급락 신호 없이 돈다."""
    eng = load_engine()
    basket = load_basket(eng, data_dir)
    end = end or (END if data_dir is None else str(basket.index[-1].date()))
    dates = basket.index[(basket.index >= start) & (basket.index <= end)]
    codes = list(basket.columns)
    if threshold_prices:
        ev.T.use_prices(threshold_prices, codes=codes)
    path = Path(contract_path)
    contract = (json.loads(path.read_text(encoding='utf-8')) if path.exists()
                else {'cases': [], 'rule_version': None, 'sample': 'missing', 'params': None})
    first = lambda verdicts, fragile=False: esc.sell_schedule(contract, verdicts, skip_fragile=fragile)
    signals = {
        'gradual': gradual_signals(codes, k, h, start, end),
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
            w = eng.run_strategy(basket, sched, recovery_rate=rr, start_date=start, end_date=end)
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
                    result[key]['placebo'] = placebo(eng, basket, sched, draws, start=start, end=end)
    return {'k': k, 'h': h, 'start': start, 'end': str(dates[-1].date()), 'stocks': len(codes),
            'threshold_prices': str(threshold_prices) if threshold_prices else None,
            'contract': {'rule_version': contract['rule_version'], 'sample': contract['sample'], 'params': contract['params']},
            'strategies': result, 'sells': sells}, pd.DataFrame(curves)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--k', type=float, default=ev.T.DEFAULT_K)
    ap.add_argument('--h', type=float, default=ev.T.DEFAULT_H)
    ap.add_argument('--out', type=Path, default=OUT)
    ap.add_argument('--draws', type=int, default=200, help='무작위 기준선 반복 수 (0이면 생략)')
    ap.add_argument('--data-dir', type=Path, help='팀 BACKTEST_DATA_DIR 형식 폴더 (없으면 19종목 표)')
    ap.add_argument('--start', default=START, help='진입일 (기본 2016-01-04)')
    ap.add_argument('--end', help='끝 (기본: 19종목은 2019-12-30, --data-dir은 데이터 끝)')
    ap.add_argument('--threshold-prices', type=Path, help='임계 모델이 읽을 KRX 형식 시세 (예: cache/krx/prices_20181201_20261116.csv)')
    ap.add_argument('--contract', type=Path, default=ROOT / 'contracts/sudden_result.json', help='급락 판정 파일 (없으면 급락 신호 없음)')
    args = ap.parse_args(argv)
    res, curves = run(args.k, args.h, args.contract, draws=args.draws, data_dir=args.data_dir, start=args.start,
                      end=args.end, threshold_prices=args.threshold_prices)
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
