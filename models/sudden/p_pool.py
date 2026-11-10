"""보도 확산 P의 합산 기준: 여러 종목의 과거 사실 묶음을 기준 분포로 쓴다.

[왜] 종목 하나의 90일에는 사실 묶음이 0~18개뿐이라 P를 낼 수 없다.
     19종목의 과거 1년 묶음을 합치면 212건 중 205건이 20개 이상이 된다.
[보정] 종목마다 평소 받는 언론사 수가 다르다. scale='stock_mean'이면 언론사 수를
     "그 종목의 과거 1년 묶음 평균 언론사 수"로 나눈 배수로 비교한다.
[시점] 기준 묶음은 사건의 base_end 전에 관찰이 끝나야 하고, 관찰한 기사가 base_end 뒤에
     수정됐으면 쓰지 않는다. 종목 평균도 같은 규칙으로 계산한다.

[실행] 212건 사건의 P를 보정 없음·보정 있음으로 비교 (N은 계산하지 않음)
    python models/sudden/p_pool.py --cache models/sudden/cache/naver_full
"""
import os
import argparse
import csv
import json
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import news_facts as facts
import news_review as review

HERE = Path(__file__).resolve().parent
OUT = HERE / os.getenv('SUDDEN_OUTPUT', 'output')     # 다른 표본(예: output_2023)을 쓸 때 바꾼다
SCALES = ('none', 'stock_mean')


def load_rows(cache):
    """종목별로, 그 종목 검색에서 나온 기업 소식 기사(시각 있음)를 모은다."""
    with (OUT / 'stock_names.csv').open(encoding='utf-8-sig', newline='') as f:
        names = defaultdict(list)
        for r in csv.DictReader(f):
            names[r['stock_code']].append(r['company_name'])
    db = sqlite3.connect(f'file:{Path(cache) / "crawl.sqlite3"}?mode=ro', uri=True)
    try:
        urls = defaultdict(set)
        for (p,) in db.execute('SELECT payload FROM searches'):
            s = json.loads(p)
            urls[s['stock_code']].update(r['naver_url'] for r in s.get('results', []) if r['naver_url'])
        articles = {a['url']: a for a in (json.loads(p) for (p,) in db.execute('SELECT payload FROM articles'))}
    finally:
        db.close()
    rows = {}
    for code, linked in urls.items():
        picked = []
        for u in sorted(linked):
            a = articles.get(u)
            if not a or a.get('status') != 'complete' or not a.get('published_at'):
                continue
            row = {**a, **review.classify(a, code, names[code])}
            if row['candidate_for_content_analysis']:
                picked.append(row)
        rows[code] = picked
    return rows


def build_pool(rows_by_stock):
    """종목별로 묶는다. 사실 묶음은 기준 분포, 모든 묶음(사실+문장)은 종목 평균에 쓴다."""
    pool = []
    for code, rows in sorted(rows_by_stock.items()):
        modified = {r['url']: r.get('modified_at') for r in rows}
        for g in facts.cluster(rows):
            pool.append({'stock_code': code, 'group_id': g['group_id'],
                         'is_fact': g.get('grouping_method') == facts.VERSION,
                         'first_published_at': g['first_published_at'],
                         'members': [{'url': m['url'], 'publisher': m.get('publisher'),
                                      'published_at': m['published_at'], 'modified_at': modified.get(m['url'])}
                                     for m in g['members']]})
    return pool


def observe(group, horizon, cutoff):
    """시작부터 horizon 동안의 언론사 수. 관찰이 cutoff 뒤에 끝나거나 정보가 부족하면 None."""
    start = datetime.fromisoformat(group['first_published_at'])
    end = start + horizon
    if end > cutoff:
        return None, 'censored'
    seen = [m for m in group['members'] if datetime.fromisoformat(m['published_at']) <= end]
    if not seen or any(not m.get('publisher') for m in seen):
        return None, 'unknown_publisher'
    if any(m.get('modified_at') and datetime.fromisoformat(m['modified_at']) > cutoff for m in seen):
        return None, 'modified_after_cutoff'
    return len({m['publisher'] for m in seen}), 'ok'


def stock_levels(pool, cutoff, days, horizon):
    """종목별 과거 묶음 평균 언론사 수 (같은 관찰시간, 같은 시점 규칙)."""
    since = cutoff - timedelta(days=days)
    counts = defaultdict(list)
    for g in pool:
        if datetime.fromisoformat(g['first_published_at']) < since:
            continue
        n, why = observe(g, horizon, cutoff)
        if why == 'ok':
            counts[g['stock_code']].append(n)
    return {code: statistics.fmean(v) for code, v in counts.items() if v}


def pooled_propagation(group, event, pool, max_hours, days=365, scale='none', min_groups=20):
    if scale not in SCALES:
        raise ValueError(f'scale은 {"/".join(SCALES)} 중 하나여야 합니다')
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError('pool_days는 양의 정수여야 합니다')
    first = datetime.fromisoformat(group['first_published_at'])
    decision = datetime.fromisoformat(event['news_window_end'])
    cutoff = datetime.fromisoformat(event['base_end'])
    horizon = min(timedelta(hours=max_hours), decision - first)
    seconds = horizon.total_seconds()
    base = {'status': 'held', 'value': None, 'baseline_scope': 'pooled', 'pool_days': days,
            'scale': scale, 'observation_seconds': seconds}
    if seconds <= 0:
        return {**base, 'hold_reason': 'no_observation_time'}
    members = [m for m in group['members'] if datetime.fromisoformat(m['published_at']) <= first + horizon]
    if any(not m.get('publisher') for m in members):
        return {**base, 'hold_reason': 'event_publisher_unknown'}
    count = len({m['publisher'] for m in members})
    levels = stock_levels(pool, cutoff, days, horizon) if scale == 'stock_mean' else {}
    if scale == 'stock_mean' and event['stock_code'] not in levels:
        return {**base, 'hold_reason': 'no_own_stock_level', 'publisher_count': count}
    own = levels.get(event['stock_code'], 1.0)
    since = cutoff - timedelta(days=days)
    refs, skipped = [], defaultdict(int)
    for g in pool:
        if not g['is_fact'] or datetime.fromisoformat(g['first_published_at']) < since:
            continue
        n, why = observe(g, horizon, cutoff)
        if why != 'ok':
            skipped[why] += 1
            continue
        if scale == 'stock_mean' and g['stock_code'] not in levels:
            skipped['no_stock_level'] += 1
            continue
        refs.append({'stock_code': g['stock_code'], 'group_id': g['group_id'], 'publisher_count': n,
                     'score': n / levels.get(g['stock_code'], 1.0)})
    me = count / own
    details = {**base, 'publisher_count': count, 'own_stock_level': levels.get(event['stock_code']),
               'event_score': round(me, 6), 'baseline_group_count': len(refs),
               'baseline_stock_count': len({r['stock_code'] for r in refs}),
               'skipped_baseline_groups': dict(skipped), 'references': refs}
    if len(refs) < min_groups:
        return {**details, 'hold_reason': 'pooled_reference_sample_too_small', 'minimum_reference_groups': min_groups}
    # 부동소수 비교 오차로 같은 배수가 다르게 세지지 않도록 반올림해서 비교한다.
    less = sum(round(r['score'], 9) < round(me, 9) for r in refs)
    ties = sum(round(r['score'], 9) == round(me, 9) for r in refs)
    return {**details, 'status': 'computed', 'hold_reason': None,
            'value': round((less + 0.5 * ties) / len(refs), 6), 'less_count': less, 'tie_count': ties,
            'formula': '(less + 0.5 * ties) / baseline_group_count' + (' on publisher_count / stock_mean' if scale == 'stock_mean' else '')}


def compare(cache, max_hours=72, days=365, min_groups=20):
    """사건 창 사실 묶음마다 보정 없음/있음 P를 계산해 차이를 요약한다."""
    rows_by_stock = load_rows(cache)
    pool = build_pool(rows_by_stock)
    with (OUT / 'drop_events.csv').open(encoding='utf-8-sig', newline='') as f:
        events = [e for e in csv.DictReader(f) if e.get('base_end')]
    out = []
    for e in events:
        window = [r for r in rows_by_stock.get(e['stock_code'], [])
                  if review.period_of(r, e) == ('event_window', 'eligible_by_timestamp')]
        for g in facts.cluster(window):
            if g.get('grouping_method') != facts.VERSION:
                continue
            a = pooled_propagation(g, e, pool, max_hours, days, 'none', min_groups)
            b = pooled_propagation(g, e, pool, max_hours, days, 'stock_mean', min_groups)
            out.append({'drop_id': e['drop_id'], 'stock_code': e['stock_code'], 'title': g['title'],
                        'action': g['primary_fact']['action'], 'publisher_count': a.get('publisher_count'),
                        'own_stock_level': b.get('own_stock_level'),
                        'P_none': a['value'], 'P_scaled': b['value'],
                        'hold_none': a['hold_reason'], 'hold_scaled': b['hold_reason'],
                        'baseline_groups': a.get('baseline_group_count')})
    both = [r for r in out if r['P_none'] is not None and r['P_scaled'] is not None]
    diffs = [abs(r['P_scaled'] - r['P_none']) for r in both]
    by_stock = defaultdict(list)
    for r in both:
        by_stock[r['stock_code']].append(r['P_scaled'] - r['P_none'])
    return {'config': {'p_observation_max_hours': max_hours, 'pool_days': days, 'min_groups': min_groups},
            'pool': {'stocks': len(rows_by_stock), 'fact_groups': sum(g['is_fact'] for g in pool),
                     'all_groups': len(pool)},
            'event_fact_groups': len(out), 'computed_both': len(both),
            'held_none': sum(r['P_none'] is None for r in out), 'held_scaled': sum(r['P_scaled'] is None for r in out),
            'abs_diff': {'median': round(statistics.median(diffs), 4), 'max': round(max(diffs), 4),
                         'over_0.2': sum(d > 0.2 for d in diffs)} if diffs else None,
            'mean_shift_by_stock': {k: round(statistics.fmean(v), 4) for k, v in sorted(by_stock.items())},
            'groups': out,
            'limits': ['합산 기준에는 수집한 날짜(사건 전 180일 구간)의 묶음만 들어 있음',
                       '같은 종목의 묶음도 기준에 포함됨',
                       '보정은 종목 평균 언론사 수로 나누는 단순 방식이며 검증된 산식이 아님',
                       'P는 네이버 수록 기사 기준이며 경제적 영향력이 아님']}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--cache', type=Path, default=HERE / 'cache/naver_full')
    ap.add_argument('--pool-days', type=int, default=365)
    ap.add_argument('--max-hours', type=float, default=72)
    args = ap.parse_args(argv)
    result = compare(args.cache, args.max_hours, args.pool_days)
    out = args.cache / 'validation'
    out.mkdir(exist_ok=True)
    path = out / 'p_pool_compare.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'groups'}, ensure_ascii=False, indent=2))
    print('저장:', path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
