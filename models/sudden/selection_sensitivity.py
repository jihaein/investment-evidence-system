"""판정이 '어떤 기사가 선정되느냐'에 얼마나 좌우되는지 잰다 (운영 하루치, live.py 결과 기준).

ACH는 사건마다 S_sel 상위 최대 12건만 LLM에 넘긴다. 외부 기사를 넣자 기사 수가 같아도 판정이 바뀌어서(2026-11-09: 22건 중 15건)
선정 자체가 판정을 흔드는지 확인한다. 판정 규칙·설정은 운영과 같다(--market-only-holds, min_support_ratio 0.25).

- 상한: 6 / 12 / 18 / 24건으로 바꿔 판정
- 하나 빼기: 선정된 12건에서 한 건씩 빼고 판정 (추가 호출 없음)
- 무작위: 상위 --pool건 중 12건을 무작위로 골라 판정 (--seeds회)

    python models/sudden/selection_sensitivity.py --date 2026-11-09 --count-only   # 추가 호출 수만 센다
    python models/sudden/selection_sensitivity.py --date 2026-11-09

결과는 cache/naver_live/ach/<날짜>_sensitivity/ (판정 이름과 숫자만, 기사 문장 없음).
"""
import argparse
import json
import os
import random
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAPS = (6, 12, 18, 24)


def setup_env(date):
    import live
    p = live.paths(date)
    os.environ.update({k: v for k, v in live.env_for(p).items()
                       if k in ('SUDDEN_OUTPUT', 'SUDDEN_PRICES', 'SUDDEN_INDEX')})
    return p


def verdict_of(ach, e, docs, classify, sessions, du, mu):
    if not docs and not du and not mu:
        return '판단보류', 'no_evidence_in_window'
    d0 = next(s for s in sessions if s['d'] == '0')
    open_time = datetime.fromisoformat(d0['trade_date'] + 'T09:00:00+09:00')
    r = ach.analyze(e, docs, classify, open_time, du, mu)
    return r['verdict'], r.get('reason') or r['hypothesis']


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--date', required=True)
    ap.add_argument('--model', default='gpt-5-mini')
    ap.add_argument('--pool', type=int, default=24, help='무작위 선정과 최대 상한에 쓰는 상위 후보 수')
    ap.add_argument('--seeds', type=int, default=10)
    ap.add_argument('--count-only', action='store_true')
    args = ap.parse_args(argv)
    p = setup_env(args.date)
    sys.path.insert(0, str(HERE))
    import ach
    import dart
    import market_rules
    ach.PARAMS.update({'market_only_holds': True, 'min_support_ratio': 0.25})
    cache = HERE / 'cache/naver_live'
    by_url, events, sessions = ach.load_inputs(cache, None)
    dart_by = dart.load_units() or {}
    market_by = market_rules.event_units(events)
    classify = ach.Classifier(args.model, cache)

    plans = []
    for e in events:
        path = cache / 'scores_all_hybrid/pooled' / f"{e['drop_id']}.json"
        sf = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'groups': []}
        if sf.get('hold_reason') == 'collection_incomplete':
            continue
        pool = ach.selection(sf['groups'], by_url, sessions[e['drop_id']], cap=args.pool)
        plans.append((e, pool))

    def cached(d, company, r):
        key = ach.hashlib.sha256(f"{d['url']}|{ach.squash(d['text'])}|{company}|{args.model}|{ach.PROMPT_VERSION}|{r}".encode()).hexdigest()
        return (classify.dir / f'{key}.json').exists()

    jobs = list({(d['url'], d['text'], e['company_name'], r): (d, e['company_name'], r)
                 for e, pool in plans for d in pool for r in range(ach.RUNS)}.values())
    missing = [j for j in jobs if not cached(*j)]
    print(json.dumps({'events': len(plans), 'pool_sizes': [len(pl) for _, pl in plans],
                      'events_over_12': sum(len(pl) > 12 for _, pl in plans),
                      'classify_jobs': len(jobs), 'new_calls_needed': len(missing)}, ensure_ascii=False))
    if args.count_only:
        return 0
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda j: classify(*j), missing))

    saved_dir = cache / 'ach' / p['tag']
    rows = []
    for e, pool in plans:
        did = e['drop_id']
        du = dart_by.get(did, {}).get('units', [])
        mu = market_by.get(did, {}).get('units', [])
        ss = sessions[did]
        v = lambda docs: verdict_of(ach, e, docs, classify, ss, du, mu)
        base = v(pool[:12])
        saved = json.loads((saved_dir / f'{did}.json').read_text(encoding='utf-8'))['verdict'] \
            if (saved_dir / f'{did}.json').exists() else None
        caps = {c: v(pool[:c])[0] for c in CAPS}
        loo = [v([d for d in pool[:12] if d is not x])[0] for x in pool[:12]] if len(pool[:12]) > 1 else []
        rnd = []
        if len(pool) > 12:
            for s in range(args.seeds):
                pick = sorted(random.Random(s).sample(pool, 12), key=lambda d: (-d['S_sel'], d['published_at']))
                rnd.append(v(pick)[0])
        rows.append({'drop_id': did, 'company': e['company_name'], 'pool': len(pool), 'base': base[0], 'base_reason': base[1],
                     'matches_saved': saved == base[0], 'caps': caps,
                     'cap_flips': sum(x != base[0] for x in caps.values()),
                     'loo_flips': sum(x != base[0] for x in loo), 'loo_n': len(loo),
                     'random': dict(Counter(rnd)), 'random_agree': (sum(x == base[0] for x in rnd) / len(rnd)) if rnd else None})
    n = len(rows)
    rnd_rows = [r for r in rows if r['random_agree'] is not None]
    summary = {
        'date': args.date, 'events': n, 'params': {k: ach.PARAMS[k] for k in ('market_only_holds', 'min_support_ratio')},
        'base_matches_saved': sum(r['matches_saved'] for r in rows),
        'verdicts_by_cap': {c: dict(Counter(r['caps'][c] for r in rows)) for c in CAPS},
        'events_any_cap_flip': sum(r['cap_flips'] > 0 for r in rows),
        'events_any_loo_flip': sum(r['loo_flips'] > 0 for r in rows),
        'loo_flip_rate': round(sum(r['loo_flips'] for r in rows) / max(1, sum(r['loo_n'] for r in rows)), 3),
        'random_events': len(rnd_rows),
        'random_mean_agree': round(sum(r['random_agree'] for r in rnd_rows) / len(rnd_rows), 3) if rnd_rows else None,
        'random_all_agree': sum(r['random_agree'] == 1 for r in rnd_rows),
        'stable_events': sum(r['cap_flips'] == 0 and r['loo_flips'] == 0 and r['random_agree'] in (None, 1) for r in rows),
        'new_calls': classify.calls, 'new_tokens': dict(classify.tokens)}
    out = cache / 'ach' / f"{p['tag']}_sensitivity"
    out.mkdir(parents=True, exist_ok=True)
    (out / 'events.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
