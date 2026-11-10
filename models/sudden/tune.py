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
GRIDS = {
    # 2026-11-10 첫 튜닝: 설정 세 가지
    'settings': {'market_only_holds': [False, True], 'positive_weight': [1.0, 0.5, 0.0], 'gap': [0.3, 0.5, 0.7]},
    # 2026-11-10 관계표 수정안: 좋은 소식은 반증만 (positive_supports=False)
    'relation': {'positive_supports': [True, False], 'market_only_holds': [False, True], 'gap': [0.5]},
    # 2026-11-10 최소 지지 비율: 반증이 적다는 이유만으로 지지가 거의 없는 가설이 이기는 경우 판단보류
    'min_support': {'market_only_holds': [True], 'min_support_ratio': [0.0, 0.15, 0.25, 0.35]},
}


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


def load_rows(ach_dir, backtest=None):
    with (Path(backtest) if backtest else Path(ach_dir) / 'backtest.csv').open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ('CAR_20', 'CAR_60'):
            r[k] = float(r[k]) if r.get(k) not in (None, '') else None
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ach', type=Path, default=HERE / 'cache/naver_2023/ach')
    ap.add_argument('--grid', choices=sorted(GRIDS), default='settings')
    ap.add_argument('--backtest', type=Path, help='사후 CAR 파일 (기본: <ach>/backtest.csv). 단위만 바뀐 재실행 폴더에 쓴다')
    args = ap.parse_args(argv)
    grid = GRIDS[args.grid]
    results = [json.loads(f.read_text(encoding='utf-8')) for f in sorted(args.ach.glob('[0-9]*_[0-9]*.json'))]   # 사건 파일만
    rows = load_rows(args.ach, args.backtest)
    log = []
    for combo in itertools.product(*grid.values()):
        params = dict(zip(grid, combo))
        log.append({'params': params, **evaluate(rows, rejudge(results, params))})
    out = {'run_at': datetime.now().isoformat(timespec='seconds'), 'ach_dir': str(args.ach), 'events': len(rows),
           'grid_name': args.grid, 'grid': grid, 'objective': 'CAR(1,60) 비가치 - 가치훼손 > 0 (비가치가 더 회복), 사건 수가 너무 적은 설정은 피한다',
           'tried': log}
    name = 'tuning_log.json' if args.grid == 'settings' else f'tuning_log_{args.grid}.json'
    (args.ach / name).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    print(' '.join(f'{k[:12]:>12}' for k in grid) + " | 가치훼손 비가치 보류 fragile | CAR60 차이   t     p   | CAR20 차이   t     p")
    for e in log:
        p, v, c6, c2 = e['params'], e['verdicts'], e['CAR_60'], e['CAR_20']
        fmt = lambda x, f: format(x, f) if x is not None else '-'
        print(' '.join(f'{str(p[k]):>12}' for k in grid) + f" | {v['가치훼손']:>6} {v['비가치']:>6} {v['판단보류']:>4} {e['fragile']:>6}"
              f" | {fmt(c6['diff'], '+.3f'):>7} {fmt(c6['t'], '.2f'):>5} {fmt(c6['p'], '.2f'):>5}"
              f" | {fmt(c2['diff'], '+.3f'):>7} {fmt(c2['t'], '.2f'):>5} {fmt(c2['p'], '.2f'):>5}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
