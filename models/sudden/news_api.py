"""NAVER API HUB 뉴스 검색으로 기사 목록을 매일 쌓고, 판정 단계가 읽는 수집 DB 형식으로 옮긴다 (docs/sudden_live_api.md).

검색 결과 페이지 크롤링(naver_crawl.py)의 '검색' 부분만 공식 API로 바꾼다. 본문은 지금처럼 naver_crawl.py --no-search로 받는다.

  1. accumulate  종목마다 최신순으로 목록을 받아 api.sqlite3에 쌓는다. 지난번에 받은 데까지만 받는다(보통 1~2번 요청).
  2. materialize 수집 계획(crawl_plan.csv)의 (종목, 날짜, 검색어)마다, API가 그날을 빠짐없이 덮었으면
                 crawl.sqlite3의 searches에 그날 기사 목록을 complete/empty로 쓴다. 덮지 못한 날은 쓰지 않는다
                 (판정 단계가 수집 미완료로 보류한다. 크롤링으로 몰래 채우지 않는다).

API는 날짜를 골라 검색할 수 없고 최신순 1,000건까지만 준다. 그래서 '덮은 기간'(가장 오래된 ~ 가장 새로운 게시 시각,
빈틈 없이 이어진 구간)을 종목·검색어마다 기록한다.

키: .env의 NAVER_API_HUB_CLIENT_ID, NAVER_API_HUB_CLIENT_SECRET. 값은 출력하지 않는다.

    python models/sudden/news_api.py accumulate --stock 005930,000660 --cache models/sudden/cache/naver_api
    python models/sudden/news_api.py materialize --cache models/sudden/cache/naver_live   # SUDDEN_OUTPUT의 crawl_plan.csv
"""
import argparse
import csv
import html
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

import naver_crawl as crawl

HERE = Path(__file__).resolve().parent
OUT = HERE / os.getenv('SUDDEN_OUTPUT', 'output')
URL = 'https://naverapihub.apigw.ntruss.com/search/v1/news'
KST = timezone(timedelta(hours=9))
PAGE, MAX_START = 100, 1000
BASELINE_CAP = 30                                    # 기준 기간 하루 최대 기사 수 (크롤링 --baseline-max-pages 3과 비슷하게)
EVENT_CAP = 100                                      # 사건 창 하루 최대 기사 수 (크롤링 --max-pages 10)
BACKOFF = (30, 120, 600)


class QuotaExceeded(RuntimeError):
    pass


class Client:
    def __init__(self, cid=None, secret=None, session=None, sleep=time.sleep, pause=0.12):
        if cid is None or secret is None:
            try:
                from dotenv import load_dotenv
                load_dotenv(HERE.parents[1] / '.env')
            except ImportError:
                pass
            cid, secret = os.getenv('NAVER_API_HUB_CLIENT_ID'), os.getenv('NAVER_API_HUB_CLIENT_SECRET')
        if not cid or not secret:
            raise SystemExit('NAVER_API_HUB_CLIENT_ID / NAVER_API_HUB_CLIENT_SECRET가 .env에 없다')
        self.headers = {'X-NCP-APIGW-API-KEY-ID': cid.strip(), 'X-NCP-APIGW-API-KEY': secret.strip()}
        self.session, self.sleep, self.pause = session or requests.Session(), sleep, pause
        self.calls = 0

    def page(self, query, start):
        params = {'query': query, 'display': PAGE, 'start': start, 'sort': 'date'}
        for attempt in range(len(BACKOFF) + 1):
            try:
                r = self.session.get(URL, params=params, headers=self.headers, timeout=20)
            except requests.RequestException:
                r = None
            self.calls += 1
            if r is not None and r.status_code == 429:
                raise QuotaExceeded('하루 호출 한도 초과(429)')
            if r is not None and r.status_code in (401, 403):
                raise SystemExit(f'API 인증 실패({r.status_code}): 키를 확인한다')
            if r is not None and r.ok:
                self.sleep(self.pause)
                return r.json().get('items', [])
            if r is not None and 400 <= r.status_code < 500:
                raise ValueError(f'API 요청 오류 {r.status_code}: {r.text[:200]}')
            if attempt < len(BACKOFF):
                self.sleep(BACKOFF[attempt])
        raise RuntimeError('API 일시 오류가 계속됨')


def clean(text):
    return html.unescape(re.sub(r'</?b>', '', text or '')).strip()


def pub_time(value):
    return parsedate_to_datetime(value).astimezone(KST)


def connect(path):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS items (link TEXT, stock_code TEXT, query TEXT, title TEXT, originallink TEXT, '
               'description TEXT, pub_at TEXT, fetched_at TEXT, PRIMARY KEY (link, stock_code, query))')
    db.execute('CREATE TABLE IF NOT EXISTS coverage (stock_code TEXT, query TEXT, oldest_at TEXT, newest_at TEXT, '
               'checked_at TEXT, PRIMARY KEY (stock_code, query))')
    db.commit()
    return db


def accumulate(client, db, stock_code, query, now=None):
    """최신순으로 받아 지난번에 덮은 시각까지 오면 멈춘다. 덮은 구간은 빈틈 없이 이어질 때만 넓힌다."""
    now = now or datetime.now(KST)
    row = db.execute('SELECT oldest_at, newest_at FROM coverage WHERE stock_code=? AND query=?', (stock_code, query)).fetchone()
    prev_old, prev_new = (datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1])) if row and row[0] else (None, None)
    sent = crawl.search_query(query)
    oldest, reached_prev, exhausted, n = None, False, False, 0
    for start in range(1, MAX_START + 1, PAGE):
        items = client.page(sent, start)
        for it in items:
            t = pub_time(it['pubDate'])
            db.execute('INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?,?,?)',
                       (it.get('link') or it.get('originallink'), stock_code, query, clean(it.get('title')),
                        it.get('originallink') or '', clean(it.get('description')), t.isoformat(), now.isoformat(timespec='seconds')))
            oldest = t if oldest is None or t < oldest else oldest
            n += 1
            if prev_new is not None and t <= prev_new:
                reached_prev = True
        if reached_prev:
            break
        if len(items) < PAGE:
            exhausted = True                          # 더 오래된 기사가 없다: 그 종목의 기사를 처음부터 다 받았다
            break
    if reached_prev:
        new_old = prev_old
    elif exhausted:
        new_old = datetime(2000, 1, 1, tzinfo=KST)    # 전부 받음
    else:
        new_old = oldest                              # 1,000건 한도에서 끊김: 이번에 받은 구간만 확실하다
    if new_old is None:                               # 결과가 하나도 없고 처음이면, 지금까지 기사 없음을 덮었다
        new_old = datetime(2000, 1, 1, tzinfo=KST)
    db.execute('INSERT OR REPLACE INTO coverage VALUES (?,?,?,?,?)',
               (stock_code, query, new_old.isoformat(), now.isoformat(), now.isoformat(timespec='seconds')))
    db.commit()
    return {'stock_code': stock_code, 'query': query, 'items': n, 'reached_prev': reached_prev, 'exhausted': exhausted}


def day_bounds(day):
    d = datetime.fromisoformat(day).replace(tzinfo=KST)
    return d, d + timedelta(days=1)


def materialize(api_db, crawl_db, plan_rows, force=False):
    """계획의 (종목, 날짜, 검색어)마다 API가 그날을 덮었으면 searches에 기록한다. 이미 complete인 크롤링 기록은 그대로 둔다."""
    written = skipped = kept = 0
    for row, query in plan_rows:
        key = json.dumps([row['stock_code'], row['date'], query], ensure_ascii=False)
        start, end = day_bounds(row['date'])
        old = crawl_db.execute('SELECT status, payload FROM searches WHERE key=?', (key,)).fetchone()
        if old and old[0] in ('complete', 'empty', 'capped') and not force:
            p = json.loads(old[1])
            # 그날 장 마감 뒤에 받아 하루가 다 차지 않았던 API 기록은 다시 쓴다(다음 날 기준 기간으로 쓰일 때 빠짐 없게)
            if not (p.get('source') == 'naver_api' and datetime.fromisoformat(p['collected_at']) < end):
                kept += 1
                continue
        cov = api_db.execute('SELECT oldest_at, newest_at FROM coverage WHERE stock_code=? AND query=?',
                             (row['stock_code'], query)).fetchone()
        # 판단 시각(15:30)까지 덮었으면 그날을 쓴다. 당일 실행(18:30)에서 사건 창을 만들 수 있게 하고, 하루가 다 차면 다음 실행에서 다시 쓴다
        need = min(end, start + timedelta(hours=15, minutes=30))
        if not cov or datetime.fromisoformat(cov[0]) > start or datetime.fromisoformat(cov[1]) < need:
            skipped += 1
            continue
        items = api_db.execute('SELECT link, title, originallink, pub_at FROM items WHERE stock_code=? AND query=? '
                               'AND pub_at >= ? AND pub_at < ? ORDER BY pub_at DESC',
                               (row['stock_code'], query, start.isoformat(), end.isoformat())).fetchall()
        cap = EVENT_CAP if int(row['priority']) == 1 else BASELINE_CAP
        results = [{'title': t, 'original_url': o or l, 'naver_url': crawl.canonical(l), 'published_at': p}
                   for l, t, o, p in items[:cap]]
        status = 'empty' if not results else ('capped' if len(items) > cap else 'complete')
        crawl.save(crawl_db, 'searches', key, status, {
            'stock_code': row['stock_code'], 'date': row['date'], 'query': query, 'search_query': crawl.search_query(query),
            'priority': int(row['priority']), 'drop_ids': row.get('drop_ids', ''), 'collected_at': crawl.now(),
            'status': status, 'results': results, 'raw_files': [], 'reason': None, 'source': 'naver_api'})
        written += 1
    return {'written': written, 'not_covered': skipped, 'kept_existing': kept}


def plan_rows(plan, stock='all', start='2000-01-01', end='2100-01-01', max_priority=3):
    args = argparse.Namespace(stock=stock, start=start, end=end, max_priority=max_priority)
    return list(crawl.tasks(plan, args))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('action', choices=['accumulate', 'materialize', 'run'],
                    help='run = 계획에 있는 종목을 accumulate한 뒤 materialize')
    ap.add_argument('--cache', type=Path, required=True, help='수집 DB 폴더 (crawl.sqlite3와 api.sqlite3를 둔다)')
    ap.add_argument('--stock', default='all', help='종목코드 쉼표 목록 또는 all(계획의 종목)')
    ap.add_argument('--plan', type=Path, default=OUT / 'crawl_plan.csv')
    ap.add_argument('--names', type=Path, default=OUT / 'stock_names.csv', help='accumulate에서 종목별 검색어(이름표의 모든 이름)')
    ap.add_argument('--start', default='2000-01-01')
    ap.add_argument('--end', default='2100-01-01')
    ap.add_argument('--force', action='store_true', help='materialize: 이미 있는 검색 기록도 API 결과로 덮어쓴다(병행 검증용 복사본에서만)')
    args = ap.parse_args(argv)
    args.cache.mkdir(parents=True, exist_ok=True)
    api_db = connect(args.cache / 'api.sqlite3')
    rows = plan_rows(args.plan, args.stock, args.start, args.end) if args.plan.exists() else []
    out = {}
    if args.action in ('accumulate', 'run'):
        if rows:
            pairs = sorted({(r['stock_code'], q) for r, q in rows})
        else:
            wanted = None if args.stock == 'all' else set(args.stock.split(','))
            pairs = sorted({(r['stock_code'], r['company_name']) for r in csv.DictReader(args.names.open(encoding='utf-8-sig'))
                            if wanted is None or r['stock_code'] in wanted})
        client = Client()
        done = []
        try:
            for code, q in pairs:
                done.append(accumulate(client, api_db, code, q))
        except QuotaExceeded as exc:
            out['quota_exceeded'] = str(exc)
        out.update(pairs=len(pairs), accumulated=len(done), api_calls=client.calls,
                   items=sum(d['items'] for d in done))
    if args.action in ('materialize', 'run'):
        cdb = crawl.connect(args.cache / 'crawl.sqlite3')
        out['materialize'] = materialize(api_db, cdb, rows, args.force)
        cdb.close()
    print(json.dumps(out, ensure_ascii=False))
    return 1 if 'quota_exceeded' in out else 0


if __name__ == '__main__':
    raise SystemExit(main())
