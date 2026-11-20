"""API 방식 병행 검증 (docs/sudden_live_api.md 10절): 매일 실행(크롤링)이 끝난 날을 API 방식으로 한 번 더 돌려 비교한다.

운영 결과는 그대로 두고 복사본에서만 돈다.
  1. cache/naver_live/crawl.sqlite3를 cache/naver_live_api/로 복사 (그날 이전 기사·평소 기사는 같은 조건)
  2. 그날 수집 계획의 검색 기록을 API 결과로 덮어쓴다 (news_api.py run --force). api.sqlite3는 매일 이어서 쌓는다
  3. 본문은 운영 캐시에 이미 있는 것은 그대로 쓰고, API에서만 나온 기사만 받는다 (naver_crawl.py --no-search)
  4. N·P → 판정 (파이널 설정 그대로, LLM 답은 운영과 같은 저장소를 써 같은 기사는 다시 묻지 않는다)
  5. 비교: 사건 창·기준 기간의 제목 회사명 기사 재현율, 판정 일치율, API 호출 수 → cache/live/<날짜>/api_compare.json

    python models/sudden/live_api_shadow.py --date 2026-11-18
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import live
import news_review as nr

HERE = Path(__file__).resolve().parent
LIVE = HERE / 'cache/naver_live'
SHADOW = HERE / 'cache/naver_live_api'


def company_links(results, name):
    return {r.get('naver_url') or r.get('original_url') for r in results if nr.name_in(name, r.get('title', ''))}


def compare(live_db, shadow_db, tag, live_ach, shadow_ach):
    by = {'event': [0, 0, 0], 'baseline': [0, 0, 0]}          # 크롤링 기사, 그중 API에도, API에만
    sdb = sqlite3.connect(shadow_db)
    ldb = sqlite3.connect(f'file:{live_db}?mode=ro', uri=True)
    for k, p in sdb.execute('SELECT key, payload FROM searches'):
        a = json.loads(p)
        if a.get('source') != 'naver_api':
            continue
        row = ldb.execute('SELECT payload FROM searches WHERE key=?', (k,)).fetchone()
        if not row:
            continue
        c = json.loads(row[0])
        cs, as_ = company_links(c.get('results', []), a['query']), company_links(a['results'], a['query'])
        g = by['event' if a['priority'] == 1 else 'baseline']
        g[0] += len(cs)
        g[1] += len(cs & as_)
        g[2] += len(as_ - cs)
    verdicts = {}
    for f in Path(live_ach).glob('*_*.json'):
        s = Path(shadow_ach) / f.name
        if s.exists():
            verdicts[f.stem] = (json.loads(f.read_text(encoding='utf-8'))['verdict'], json.loads(s.read_text(encoding='utf-8'))['verdict'])
    same = sum(a == b for a, b in verdicts.values())
    return {'date_tag': tag,
            'recall': {g: {'crawl_articles': v[0], 'also_in_api': v[1], 'api_only': v[2],
                           'recall': round(v[1] / v[0], 3) if v[0] else None} for g, v in by.items()},
            'verdict_agree': round(same / len(verdicts), 3) if verdicts else None, 'events_compared': len(verdicts),
            'verdict_diffs': {k: {'crawl': a, 'api': b} for k, (a, b) in verdicts.items() if a != b}}


def run(date, runner=subprocess.run):
    p = live.paths(date)
    status = p['out'] / 'run_status.json'
    if not status.exists() or json.loads(status.read_text(encoding='utf-8')).get('result') != 'done':
        print(f'{date}: 매일 실행이 끝나지 않아 병행 검증을 건너뛴다')
        return None
    SHADOW.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(LIVE / 'crawl.sqlite3', SHADOW / 'crawl.sqlite3')
    env = {**live.env_for(p), 'SUDDEN_LLM_CACHE_DIR': str(LIVE)}
    rel = lambda path: str(Path(path).relative_to(HERE))
    ach_out = SHADOW / 'ach' / p['tag']
    body = [c for c in live.commands(date, p, 'api')['crawl'] if c[0] == 'naver_crawl.py'][0]
    body = [x if x != rel(live.NEWS_CACHE) else rel(SHADOW) for x in body]
    steps = [['news_api.py', 'run', '--cache', rel(SHADOW), '--start', p['news_start'], '--end', date, '--force'],
             body,
             ['np_batch.py', '--cache', rel(SHADOW)],
             ['ach.py', '--cache', rel(SHADOW), '--model', 'gpt-5-mini', '--market-only-holds', '--min-support-ratio', '0.25',
              '--out', rel(ach_out)]]
    api_calls = None
    for cmd in steps:
        print('[shadow]', ' '.join(cmd), flush=True)
        res = runner([live.PY, *cmd], cwd=HERE, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if cmd[0] == 'news_api.py' and res.stdout:
            try:
                api_calls = json.loads(res.stdout.strip().splitlines()[-1]).get('api_calls')
            except ValueError:
                pass
        if res.returncode != 0:
            out = {'date': date, 'failed_step': cmd[0], 'stderr_tail': (res.stderr or '')[-800:]}
            (p['out'] / 'api_compare.json').write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
            print(json.dumps(out, ensure_ascii=False))
            return out
    out = {'date': date, 'api_calls': api_calls,
           **compare(LIVE / 'crawl.sqlite3', SHADOW / 'crawl.sqlite3', p['tag'], p['ach'], ach_out)}
    (p['out'] / 'api_compare.json').write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(out, ensure_ascii=False))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--date', required=True, help='YYYY-MM-DD 또는 today')
    args = ap.parse_args(argv)
    run(live.resolve_date(args.date))
    return 0


if __name__ == '__main__':
    sys.exit(main())
