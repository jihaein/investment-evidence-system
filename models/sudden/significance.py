"""significance.py — 파이널 시험(120종목 2020~2026) 결과가 우연과 구별되는지 본다. 판정·백테스트는 다시 하지 않는다.

[A] 사건 연구 (주 검정)
    급락 사건마다 매도일 = 급락일 다음 거래일(스트레스 lag 1과 같다). 그 종가 이후 H 거래일 동안
    종목 누적수익률 - 같은 기간 120종목 동일가중 누적수익률 = 초과수익(AR).
    가치훼손이 맞다면 매도 뒤 AR이 음수여야 하고(팔아서 피한 손실), 비가치보다 낮아야 한다.
    같은 종목 사건끼리는 독립이 아니므로 종목 단위로 묶어 재표집한다(클러스터 부트스트랩).

[B] 스트레스 런 (보조)
    stress_detail(lag 1)의 런별 차이: 급락 - 무손절, 급락 - 같은 수 무작위 매도.
    런끼리 진입 기간이 겹치므로 진입 분기 단위로 묶어 재표집한다. 보유 기간이 분기보다 길어 이웃 분기도
    겹치므로, 이 구간은 여전히 낙관적이다(실제 불확실성은 더 크다).

[C] 분류 대조군 (--controls)
    같은 런(진입일·바스켓이 같은 seed 7)에서 판정 대신 다른 규칙으로 판다.
      아무급락 : 판정과 상관없이 종목별 첫 급락일 다음 거래일에 판다 (분류가 없을 때)
      비가치급락: 첫 '비가치' 급락에 판다 (분류가 반대로 말할 때)
    급락(가치훼손)이 이 둘보다 나아야 분류에 값어치가 있다. 임계는 계산하지 않는다(alarm_fn 빈 값).

p값은 한쪽(전략이 낫다는 쪽) 부트스트랩 근사: 재표집 평균이 0 이하(또는 이상)로 나온 비율.

    .venv\\Scripts\\python.exe models\\sudden\\significance.py
"""
import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

import joint_backtest as jb
import stress

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CONTRACT = ROOT / 'contracts/sudden_result_team120_2020_2026.json'
DETAIL = HERE / 'cache/stress_120_final/stress_detail_k0.5_h5_seed7.csv'
OUT = HERE / 'cache/significance_20261121'
HORIZONS = (20, 60, 120, 250)          # 거래일: 약 1·3·6·12개월
N_BOOT = 10_000


def forward_ar(grid, cases, horizon):
    """사건별 매도일 이후 horizon 거래일 초과수익. 창이 데이터 끝을 넘는 사건은 뺀다."""
    growth = 1 + grid / 100
    market = 1 + grid.mean(axis=1, skipna=True) / 100
    days = grid.index
    rows = []
    for c in cases:
        code = c['stock_code']
        if code not in grid.columns:
            continue
        sell = jb.next_trading_day(days, c['drop_date'])
        if sell is None:
            continue
        i = days.get_loc(sell)
        if i + horizon >= len(days):
            continue
        stock = growth[code].iloc[i + 1:i + 1 + horizon]
        if stock.isna().any():
            continue
        r_s = stock.prod() - 1
        r_m = market.iloc[i + 1:i + 1 + horizon].prod() - 1
        rows.append({'stock_code': code, 'drop_date': c['drop_date'], 'verdict': c['verdict'], 'ar': r_s - r_m})
    return pd.DataFrame(rows)


def cluster_boot_mean(values, clusters, rng, n=N_BOOT):
    """클러스터(종목·분기)를 복원추출해 평균의 분포를 만든다."""
    df = pd.DataFrame({'v': values, 'g': clusters})
    sums = df.groupby('g')['v'].agg(['sum', 'count'])
    s, k = sums['sum'].to_numpy(), sums['count'].to_numpy()
    idx = rng.integers(0, len(sums), size=(n, len(sums)))
    return s[idx].sum(axis=1) / k[idx].sum(axis=1)


def describe(boot, point, better='neg'):
    lo, hi = np.percentile(boot, [2.5, 97.5])
    p = float((boot >= 0).mean() if better == 'neg' else (boot <= 0).mean())
    return {'mean': float(point), 'ci95': [float(lo), float(hi)], 'p_one_sided': p}


def event_study(grid, cases, rng):
    out = {}
    for h in HORIZONS:
        ev = forward_ar(grid, cases, h)
        rec = {'n': ev['verdict'].value_counts().to_dict()}
        for v in ('가치훼손', '비가치', '판단보류'):
            e = ev[ev['verdict'] == v]
            rec[v] = {**describe(cluster_boot_mean(e['ar'], e['stock_code'], rng), e['ar'].mean()),
                      'median': float(e['ar'].median()), 'share_negative': float((e['ar'] < 0).mean()),
                      'n_events': int(len(e)), 'n_stocks': int(e['stock_code'].nunique())}
        a, b = ev[ev['verdict'] == '가치훼손'], ev[ev['verdict'] == '비가치']
        boot = cluster_boot_mean(a['ar'], a['stock_code'], rng) - cluster_boot_mean(b['ar'], b['stock_code'], rng)
        rec['가치훼손-비가치'] = describe(boot, a['ar'].mean() - b['ar'].mean())
        out[h] = rec
    return out


def stress_runs(detail, rng):
    d = detail[(detail['lag'] == 1) | detail['strategy'].str.startswith('무작위매도_')]
    out = {}
    for hz, g in d.groupby('horizon'):
        R = g.pivot_table(index='run', columns='strategy', values='누적수익률')
        entry = pd.to_datetime(g.groupby('run')['entry'].first())
        quarter = entry.dt.year.astype(str) + 'Q' + entry.dt.quarter.astype(str)
        rec = {'runs': int(len(R)), 'entry_quarters': int(quarter.nunique())}
        for name, base in (('급락-무손절', '무손절'), ('급락-무작위매도', '무작위매도_급락'), ('임계-무손절', '무손절')):
            s = name.split('-')[0]
            diff = (R[s] - R[base]).dropna()
            boot = cluster_boot_mean(diff.to_numpy(), quarter.loc[diff.index].to_numpy(), rng)
            rec[name] = {**describe(boot, diff.mean(), better='pos'), 'win_rate': float((diff > 0).mean())}
        out[int(hz)] = rec
    return out


CONTROLS = {'아무급락': ('가치훼손', '비가치', '판단보류'), '비가치급락': ('비가치',)}


def control_runs(eng, grid, cases, detail, rng, horizons=(1, 2, 3, 4), runs=150):
    """대조 규칙의 매도일을 '가치훼손'으로 바꿔 넣어 stress.run_stress를 같은 seed로 다시 돌린다."""
    base = detail[(detail['lag'] == 1) & (detail['strategy'] == '급락')].set_index(['horizon', 'run'])
    out = {}
    for name, verdicts in CONTROLS.items():
        relabeled = [{**c, 'verdict': '가치훼손' if c['verdict'] in verdicts else '제외'} for c in cases]
        parts = [stress.run_stress(eng, grid, n_runs=runs, n_pick=20, horizon_years=h, seed=7, lags=(1,), n_rand=0,
                                   cases=relabeled, alarm_fn=lambda codes, entry, end: {}) for h in horizons]
        d = pd.concat(parts, ignore_index=True)
        d = d[(d['strategy'] == '급락')].set_index(['horizon', 'run'])
        assert (pd.to_datetime(d['entry']) == pd.to_datetime(base.loc[d.index, 'entry'])).all(), '런 구성이 다르다'
        rec = {}
        for h in horizons:
            a, b = base.loc[h], d.loc[h]
            diff = a['누적수익률'] - b['누적수익률']
            entry = pd.to_datetime(a['entry'])
            quarter = (entry.dt.year.astype(str) + 'Q' + entry.dt.quarter.astype(str)).to_numpy()
            rec[h] = {**describe(cluster_boot_mean(diff.to_numpy(), quarter, rng), diff.mean(), better='pos'),
                      'win_rate': float((diff > 0).mean()), 'ties': float((diff == 0).mean()),
                      'control_return_mean': float(b['누적수익률'].mean()), 'sells_median': float(b['매도수'].median())}
        out[f'급락-{name}'] = rec
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--data-dir', type=Path, help='팀 120종목 2020-2026 폴더 (없으면 .env의 BACKTEST_DATA_DIR_2026)')
    ap.add_argument('--seed', type=int, default=20261121)
    ap.add_argument('--out', type=Path, default=OUT)
    ap.add_argument('--controls', action='store_true', help='[C] 분류 대조군도 돌린다 (수십 분)')
    args = ap.parse_args(argv)
    load_dotenv(ROOT / '.env')
    data_dir = args.data_dir or os.environ.get('BACKTEST_DATA_DIR_2026')
    if not data_dir:
        ap.error('--data-dir 또는 BACKTEST_DATA_DIR_2026이 필요하다')
    rng = np.random.default_rng(args.seed)
    eng = jb.load_engine()
    grid = jb.load_basket(eng, data_dir)
    cases = json.loads(CONTRACT.read_text(encoding='utf-8'))['cases']
    detail = pd.read_csv(DETAIL, encoding='utf-8-sig', dtype={'entry': str})
    res = {'seed': args.seed, 'n_boot': N_BOOT, 'contract': CONTRACT.name, 'stress_detail': DETAIL.name,
           'event_study_trading_days': event_study(grid.sort_index(), cases, rng),
           'stress_runs_lag1': stress_runs(detail, rng)}
    if args.controls:
        res['classification_controls_lag1'] = control_runs(eng, grid, cases, detail, rng)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'significance.json').write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
