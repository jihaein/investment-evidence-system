"""사후 주가 검증 (설계서 v3.1 7절): ACH 판정 그룹별로 급락 뒤 누적 비정상수익률(CAR)과 회복률을 비교한다.

가치훼손 판정은 정보가 가격에 반영된 하락이라 회복이 적고, 비가치 판정은 일시 충격이라 회복이 커야 한다.
- AR은 시장모형: D-120 ~ D-1 거래일 수익률을 시장지수 수익률에 회귀해 α·β를 구하고 AR = r - (α + β·r_m).
- CAR(1,k) = D+1 ~ D+k 거래일 AR 합. 회복률 = CAR(1,k) / |AR_D| (1이면 급락폭만큼 회복).
- 그룹 차이(비가치 - 가치훼손)는 급락일(D) 단위로 군집한 표준오차의 t값과, 같은 날 사건을 한 블록으로 섞는 순열검정 p값으로 본다.
- 주가 파일이 2019-12-30까지라 D+k가 그 뒤인 사건은 그 k에서 뺀다(censored).

[주의] 설계서는 2023~2024 튜닝 / 2025 검증 표본을 쓴다. 지금은 2015~2019년 19종목이고 튜닝·검증 분리가 없어 탐색용이다.

[실행]
    python models/sudden/backtest.py                                   # cache/naver_full/ach 판정 사용
    python models/sudden/backtest.py --ach models/sudden/cache/naver_full/ach_nano_v2
"""
import argparse
import csv
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

import numpy as np

import events as ev

HERE = Path(__file__).resolve().parent
HORIZONS = (5, 20, 60)
EST_WINDOW, MIN_EST = 120, 60
GROUPS = ('가치훼손', '비가치', '판단보류')


def market_model_car(stock, d_index, horizons=HORIZONS):
    """stock: 한 종목의 거래일순 DataFrame(ret, index_ret, halted). d_index: 급락일 행 번호."""
    est = stock.iloc[max(0, d_index - EST_WINDOW):d_index]
    est = est[~est['halted'] & est['ret'].notna() & est['index_ret'].notna()]
    if len(est) < MIN_EST:
        return None
    beta, alpha = np.polyfit(est['index_ret'].to_numpy(float), est['ret'].to_numpy(float), 1)
    resid_sd = float(np.std(est['ret'] - (alpha + beta * est['index_ret']), ddof=2))

    def ar(i):
        row = stock.iloc[i]
        return float(row['ret'] - (alpha + beta * row['index_ret']))
    ar_d = ar(d_index)
    out = {'alpha': alpha, 'beta': float(beta), 'resid_sd': resid_sd, 'AR_D': ar_d, 'z_market_model': ar_d / resid_sd}
    for k in horizons:
        if d_index + k >= len(stock):
            out[f'CAR_{k}'] = None
            continue
        car = sum(ar(i) for i in range(d_index + 1, d_index + k + 1))
        out[f'CAR_{k}'] = car
        out[f'recovery_{k}'] = car / abs(ar_d) if ar_d else None
    return out


def clustered_t(values_a, values_b, clusters_a, clusters_b):
    """평균 차이(b - a)와 급락일 군집 표준오차 t값. 회귀 y = c + d·[b]에 CR1 군집 분산을 쓴다."""
    y = np.array(values_a + values_b, float)
    x = np.array([0] * len(values_a) + [1] * len(values_b), float)
    g = clusters_a + clusters_b
    X = np.column_stack([np.ones_like(x), x])
    xtx_inv = np.linalg.inv(X.T @ X)
    coef = xtx_inv @ X.T @ y
    resid = y - X @ coef
    meat = np.zeros((2, 2))
    ids = sorted(set(g))
    for c in ids:
        idx = [i for i, gi in enumerate(g) if gi == c]
        s = X[idx].T @ resid[idx]
        meat += np.outer(s, s)
    n, k, m = len(y), 2, len(ids)
    scale = (m / (m - 1)) * ((n - 1) / (n - k)) if m > 1 else 1.0
    se = float(np.sqrt((xtx_inv @ meat @ xtx_inv)[1, 1] * scale))
    return float(coef[1]), se, (float(coef[1]) / se if se else None), m


def block_permutation(values, labels, clusters, n=5000, seed=0):
    """같은 급락일 사건은 한 블록으로 묶어 블록 단위로 라벨을 섞는다. 양측 p값."""
    blocks = defaultdict(list)
    for v, l, c in zip(values, labels, clusters):
        blocks[c].append((v, l))
    keys = sorted(blocks)
    block_labels = [[l for _, l in blocks[k]] for k in keys]

    def diff(assign):
        a = [v for k, labs in zip(keys, assign) for (v, _), l in zip(blocks[k], labs) if l == 0]
        b = [v for k, labs in zip(keys, assign) for (v, _), l in zip(blocks[k], labs) if l == 1]
        return statistics.fmean(b) - statistics.fmean(a) if a and b else 0.0
    observed = diff(block_labels)
    rng, hits = random.Random(seed), 0
    for _ in range(n):
        # 블록 크기가 다르면 라벨 묶음을 같은 크기 블록끼리만 바꾼다
        by_size = defaultdict(list)
        for labs in block_labels:
            by_size[len(labs)].append(labs)
        for size in by_size:
            rng.shuffle(by_size[size])
        it = {size: iter(v) for size, v in by_size.items()}
        assign = [next(it[len(labs)]) for labs in block_labels]
        hits += abs(diff(assign)) >= abs(observed) - 1e-12
    return observed, (hits + 1) / (n + 1)


def summarize(rows, key):
    out = {}
    for grp in GROUPS:
        vals = [r[key] for r in rows if r['verdict'] == grp and r.get(key) is not None]
        out[grp] = {'n': len(vals), 'mean': round(statistics.fmean(vals), 4) if vals else None,
                    'median': round(statistics.median(vals), 4) if vals else None}
    a = [r for r in rows if r['verdict'] == '가치훼손' and r.get(key) is not None]
    b = [r for r in rows if r['verdict'] == '비가치' and r.get(key) is not None]
    if len(a) >= 2 and len(b) >= 2:
        d, se, t, m = clustered_t([r[key] for r in a], [r[key] for r in b], [r['D'] for r in a], [r['D'] for r in b])
        values = [r[key] for r in a + b]
        labels = [0] * len(a) + [1] * len(b)
        _, p = block_permutation(values, labels, [r['D'] for r in a + b])
        out['비가치_minus_가치훼손'] = {'diff': round(d, 4), 'clustered_se': round(se, 4),
                                     't': round(t, 2) if t is not None else None, 'clusters': m,
                                     'permutation_p': round(p, 4)}
    return out


def run(ach_dir):
    prices = ev.load_prices()
    index = ev.load_index(prices)
    df = prices.merge(index, on=['trade_date', 'market'], how='left')
    df['halted'] = df['volume'].fillna(0) == 0
    by_stock = {c: g.sort_values('trade_date').reset_index(drop=True) for c, g in df.groupby('stock_code')}
    rows = []
    for f in sorted(Path(ach_dir).glob('*_*.json')):
        r = json.loads(f.read_text(encoding='utf-8'))
        stock = by_stock.get(r['stock_code'])
        if stock is None:
            continue
        hit = stock.index[stock['trade_date'] == np.datetime64(r['D'])]
        if not len(hit):
            continue
        car = market_model_car(stock, int(hit[0]))
        if car is None:
            continue
        rows.append({'drop_id': r['drop_id'], 'stock_code': r['stock_code'], 'D': r['D'], 'verdict': r['verdict'],
                     'hypothesis': r['hypothesis'], 'reason': r.get('reason'),
                     'fragile': bool((r.get('flags') or {}).get('fragile')), **car})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ach', type=Path, default=HERE / 'cache/naver_full/ach')
    ap.add_argument('--out', type=Path)
    args = ap.parse_args(argv)
    rows = run(args.ach)
    robust = [r for r in rows if not r['fragile']]
    result = {'ach_dir': str(args.ach), 'events': len(rows),
              'verdicts': {g: sum(r['verdict'] == g for r in rows) for g in GROUPS},
              'by_horizon': {f'CAR_{k}': summarize(rows, f'CAR_{k}') for k in HORIZONS},
              'recovery': {f'recovery_{k}': summarize(rows, f'recovery_{k}') for k in HORIZONS},
              'non_fragile_only': {f'CAR_{k}': summarize(robust, f'CAR_{k}') for k in (20, 60)},
              'AR_D': summarize(rows, 'AR_D'),
              'limits': ['2015~2019년 19종목, 튜닝·검증 표본 분리 없음(탐색용)',
                         'ACH 입력 일부가 임시값(V=1, DART·시장 규칙 없음)',
                         '주가 파일이 2019-12-30까지라 늦은 사건은 긴 구간에서 빠짐',
                         '그룹 평균만 해석하고 개별 사건 정답으로 쓰지 않음']}
    out = args.out or args.ach / 'backtest.json'
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    with out.with_suffix('.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(json.dumps({k: v for k, v in result.items() if k != 'limits'}, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
