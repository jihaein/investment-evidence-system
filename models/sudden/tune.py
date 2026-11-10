"""ACH 판정 설정 튜닝 (설계서 v3.1 7절: 튜닝은 2023~2024 표본에서만, 시도한 설정은 결과와 상관없이 모두 기록).

저장된 ACH 단위(cache/.../ach/*.json)를 설정만 바꿔 다시 판정하고, 같은 사건의 사후 CAR(backtest.csv)로 비교한다.
LLM을 다시 부르지 않는다. 격자는 미리 정해 두고 전부 돌린다(결과를 보고 격자를 넓히면 그것도 기록한다).

    python models/sudden/tune.py --ach models/sudden/cache/naver_2023/ach

출력: <ach>/tuning_log.json (설정마다 판정 수, CAR 차이, t, 순열 p, fragile 수)
"""
import argparse
import csv
import itertools
import json
from datetime import datetime
from pathlib import Path

import ach
import backtest as bt

HERE = Path(__file__).resolve().parent
GRID = {'market_only_holds': [False, True], 'positive_weight': [1.0, 0.5, 0.0], 'gap': [0.3, 0.5, 0.7]}


def rejudge(results, params):
    out = {}
    for r in results:
        units = r.get('units') or []
        if not units or r.get('reason') == 'collection_incomplete':
            out[r['drop_id']] = ('판단보류', False)
            continue
        main, flags = ach.judge(units, params=params)
        out[r['drop_id']] = (main['verdict'], flags['fragile'])
    return out


def evaluate(rows, verdicts):
    rows = [{**r, 'verdict': verdicts[r['drop_id']][0], 'fragile': verdicts[r['drop_id']][1]} for r in rows]
    res = {'verdicts': {g: sum(r['verdict'] == g for r in rows) for g in bt.GROUPS},
           'fragile': sum(r['fragile'] and r['verdict'] != '판단보류' for r in rows)}
    for k in ('CAR_20', 'CAR_60'):
        s = bt.summarize(rows, k)
        d = s.get('비가치_minus_가치훼손', {})
        res[k] = {'value_impair_mean': s['가치훼손']['mean'], 'non_value_mean': s['비가치']['mean'],
                  'diff': d.get('diff'), 't': d.get('t'), 'p': d.get('permutation_p')}
    return res


def load_rows(ach_dir):
    with (Path(ach_dir) / 'backtest.csv').open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ('CAR_20', 'CAR_60'):
            r[k] = float(r[k]) if r.get(k) not in (None, '') else None
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ach', type=Path, default=HERE / 'cache/naver_2023/ach')
    args = ap.parse_args(argv)
    results = [json.loads(f.read_text(encoding='utf-8')) for f in sorted(args.ach.glob('*_*.json'))]
    rows = load_rows(args.ach)
    log = []
    for combo in itertools.product(*GRID.values()):
        params = dict(zip(GRID, combo))
        log.append({'params': params, **evaluate(rows, rejudge(results, params))})
    out = {'run_at': datetime.now().isoformat(timespec='seconds'), 'ach_dir': str(args.ach), 'events': len(rows),
           'grid': GRID, 'objective': 'CAR(1,60) 비가치 - 가치훼손 > 0 (비가치가 더 회복), 사건 수가 너무 적은 설정은 피한다',
           'tried': log}
    (args.ach / 'tuning_log.json').write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f"{'market_only':>11} {'pos_w':>5} {'gap':>4} | 가치훼손 비가치 보류 fragile | CAR60 차이   t     p   | CAR20 차이   t     p")
    for e in log:
        p, v, c6, c2 = e['params'], e['verdicts'], e['CAR_60'], e['CAR_20']
        fmt = lambda x, f: format(x, f) if x is not None else '-'
        print(f"{str(p['market_only_holds']):>11} {p['positive_weight']:>5} {p['gap']:>4} | {v['가치훼손']:>6} {v['비가치']:>6} {v['판단보류']:>4} {e['fragile']:>6}"
              f" | {fmt(c6['diff'], '+.3f'):>7} {fmt(c6['t'], '.2f'):>5} {fmt(c6['p'], '.2f'):>5}"
              f" | {fmt(c2['diff'], '+.3f'):>7} {fmt(c2['t'], '.2f'):>5} {fmt(c2['p'], '.2f'):>5}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
