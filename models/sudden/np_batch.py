"""여러 사건의 2차 점수(N·P)를 한 번에 계산한다 (규칙 + LLM 섞음, llm_facts.Hybrid).

2015~2019·2023~2025 표본에서 쓰던 임시 스크립트를 옮겼다. 크롤 DB(crawl.sqlite3)는 한 번만 읽는다.
결과: <cache>/scores_all_hybrid/{own,pooled}/<drop_id>.json (ach.py가 pooled를 읽는다)

    $env:SUDDEN_OUTPUT='output_2023'; python models/sudden/np_batch.py --cache models/sudden/cache/naver_2023 --split tune
"""
import argparse
import csv
import json
import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import llm_facts as lf
import news_facts as facts
import news_review as review
import np_scores
from external_article import result_urls
import p_pool

HERE = Path(__file__).resolve().parent
OUT = HERE / os.getenv('SUDDEN_OUTPUT', 'output')
DONE = ('complete', 'excluded_automated', 'excluded_title_mismatch')


def load_crawl(cache):
    db = sqlite3.connect(f'file:{Path(cache) / "crawl.sqlite3"}?mode=ro', uri=True)
    try:
        searches = [json.loads(p) for (p,) in db.execute('SELECT payload FROM searches')]
        articles = {a['url']: a for a in (json.loads(p) for (p,) in db.execute('SELECT payload FROM articles'))}
    finally:
        db.close()
    return searches, articles


def report_for(e, by_stock, searches, articles, names):
    ss = [s for s in by_stock[e['stock_code']] if e['n_base_start'][:10] <= s['date'] <= e['news_window_end'][:10]]
    urls = result_urls([r for s in ss for r in s.get('results', [])], articles)
    arts = [articles[u] for u in urls if u in articles]
    reviewed, datasets = review.prepare(arts, e, names[e['stock_code']])
    cover = review.coverage(searches, e)
    pending = urls - {a['url'] for a in arts if a.get('status') in DONE}
    cover['unavailable_article_count'] = len(pending)
    rep = {'drop_id': e['drop_id'], 'event': e, 'coverage': cover, 'datasets': datasets,
           'ready_for_baseline_analysis': cover['search_coverage_complete'] and not pending}
    return rep, reviewed


def run(cache, split=None, drop_ids=None, out_dir=OUT):
    cache = Path(cache)
    lf.load_env()
    hybrid = lf.Hybrid(lf.Extractor(lf.DEFAULT_MODEL, cache))
    facts.extract = hybrid
    searches, articles = load_crawl(cache)
    by_stock = defaultdict(list)
    for s in searches:
        by_stock[s['stock_code']].append(s)
    names = defaultdict(list)
    with (out_dir / 'stock_names.csv').open(encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            names[r['stock_code']].append(r['company_name'])
    with (out_dir / 'drop_events.csv').open(encoding='utf-8-sig') as f:
        events = [e for e in csv.DictReader(f) if (not split or e.get('split') == split)
                  and (not drop_ids or e['drop_id'] in drop_ids)]
    t = time.time()
    pool = p_pool.build_pool(p_pool.load_rows(cache))
    cfgs = {k: json.loads((HERE / f).read_text(encoding='utf-8')) for k, f in (('own', 'np_config.json'), ('pooled', 'np_config_pooled.json'))}
    out = cache / 'scores_all_hybrid'
    summary = []
    for i, e in enumerate(events):
        if not e.get('base_end'):
            summary.append({'drop_id': e['drop_id'], 'skip': 'no_base_end'})
            continue
        rep, rows = report_for(e, by_stock, searches, articles, names)
        row = {'drop_id': e['drop_id'], 'stock_code': e['stock_code'], 'ready': rep['ready_for_baseline_analysis']}
        for k, cfg in cfgs.items():
            r = np_scores.score(rep, rows, cfg, pool if k == 'pooled' else None)
            (out / k).mkdir(parents=True, exist_ok=True)
            (out / k / f"{e['drop_id']}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1, allow_nan=False), encoding='utf-8')
            row[k] = {'status': r['status'], 'hold': r.get('hold_reason'), 'groups': len(r['groups'])}
        summary.append(row)
        if i % 20 == 0:
            print(i, e['drop_id'], round(time.time() - t), 's', flush=True)
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
    return {'events': len(events), 'pool': len(pool), 'seconds': round(time.time() - t),
            'used': dict(hybrid.used), 'new_llm_calls': hybrid.llm.calls}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--cache', type=Path, default=HERE / 'cache/naver_full')
    ap.add_argument('--split')
    ap.add_argument('--drop-id', action='append')
    args = ap.parse_args(argv)
    print(json.dumps(run(args.cache, args.split, set(args.drop_id or [])), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
