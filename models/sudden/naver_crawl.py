"""네이버 과거 뉴스 수집. 기본 실행은 현진소재 2018-12-14~19 파일럿.

검색 HTML/JSON과 기사 HTML을 cache/naver에 보관하고 SQLite로 진행 상태를 기록한다.
외부 언론사 링크는 검색 기록에 남기되 본문은 네이버에 수록된 기사만 수집한다.
--stock all(또는 쉼표 목록)이면 여러 종목을 우선순위·날짜순으로 섞어서 수집한다.
검색어는 기본으로 따옴표로 감싸 정확히 일치하는 기사만 찾는다(--no-quote로 이전 방식).
"""
import argparse
import csv
import hashlib
import json
import re
import queue
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

HERE = Path(__file__).resolve().parent
TERMS_PATH = HERE / 'output/search_terms.csv'
TERM_COLUMNS = ('약칭', '영문명', '주요_종속회사')  # 팀이 채우는 추가 검색어 칸. 여러 개는 | 로 구분
KST = timezone(timedelta(hours=9))
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


class ParseError(ValueError):
    pass


class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def nodes(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.nodes()

    def text(self):
        if self.tag in ('script', 'style'):
            return ''
        if self.tag == 'br':
            return ' '
        text = ''.join(c.text() if isinstance(c, Node) else c for c in self.children)
        return text + (' ' if self.tag in ('p', 'div') else '')


class Tree(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def tidy(text):
    return re.sub(r'\s+', ' ', text).strip()


def canonical(url):
    p = urlparse(url)
    if p.hostname not in ('n.news.naver.com', 'news.naver.com', 'm.news.naver.com'):
        return None
    match = re.fullmatch(r'/(?:mnews/)?article/(\d{3})/(\d+)', p.path)
    if match:
        oid, aid = match.groups()
    else:
        q = parse_qs(p.query)
        if p.path not in ('/main/read.naver', '/main/read.nhn') or not q.get('oid') or not q.get('aid'):
            return None
        oid, aid = q['oid'][0], q['aid'][0]
        if not re.fullmatch(r'\d{3}', oid) or not aid.isdigit():
            return None
    return f'https://n.news.naver.com/mnews/article/{oid}/{aid}'


def title_link(node):
    return node.tag == 'a' and (node.attrs.get('data-heatmap-target') == '.tit'
                               or 'news_tit' in node.attrs.get('class', '').split())


def parse_search(html):
    tree = Tree(html)
    results = []
    for node in tree.root.nodes():
        if not title_link(node):
            continue
        original = node.attrs.get('href', '')
        if urlparse(original).scheme not in ('http', 'https'):
            continue
        nav = canonical(original)
        parent = node.parent
        # 가장 가까운 단일 기사 컨테이너에서 네이버 수록 링크를 찾는다.
        while parent is not None:
            children = list(parent.nodes())
            if sum(title_link(n) for n in children) > 1:
                break
            links = [canonical(n.attrs.get('href', '')) for n in children if n.tag == 'a']
            nav = next((u for u in links if u), nav)
            if nav:
                break
            parent = parent.parent
        # 제목 링크 안의 접근성 문구 '새 창 열림'은 제목이 아니다
        title = re.sub(r'\s*새 창 열림$', '', tidy(node.text()))
        results.append({'title': title, 'original_url': original, 'naver_url': nav})
    # 구형 목록에서 제목 링크가 없더라도 네이버 기사 주소는 보존한다.
    if not results:
        urls = {canonical(n.attrs.get('href', '')) for n in tree.root.nodes() if n.tag == 'a'} - {None}
        results = [{'title': '', 'original_url': u, 'naver_url': u} for u in sorted(urls)]
    empty = bool(re.search(r'검색결과가\s*없습니다', tidy(tree.root.text())))
    if empty and results:
        raise ParseError('검색 결과와 결과 없음 문구가 동시에 있음')
    if not results and not empty:
        raise ParseError('검색 목록을 인식하지 못함 (차단 또는 구조 변경 가능)')
    next_url = None
    for more in re.finditer(r'"url"\s*:\s*("(?:[^"\\]|\\.)*")', html):
        candidate = json.loads(more[1])
        if urlparse(candidate).hostname == 's.search.naver.com' and '/api/tab/more' in candidate:
            next_url = candidate
            break
    if not next_url:
        for n in tree.root.nodes():
            if n.tag == 'a' and 'btn_next' in n.attrs.get('class', '').split() and n.attrs.get('aria-disabled') != 'true':
                from urllib.parse import urljoin
                next_url = urljoin('https://search.naver.com/search.naver', n.attrs.get('href', ''))
                break
    return results, next_url, empty


def timestamp(value):
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (d.replace(tzinfo=KST) if d.tzinfo is None else d.astimezone(KST)).isoformat()
    except ValueError:
        return None


def parse_article(html, url):
    nodes = list(Tree(html).root.nodes())
    meta = {n.attrs.get('property', n.attrs.get('name')): n.attrs.get('content') for n in nodes if n.tag == 'meta'}
    body = next((n for n in nodes if n.attrs.get('id') in ('dic_area', 'articleBodyContents', 'newsEndContents')), None)
    title = meta.get('og:title')
    if not title or body is None or not tidy(body.text()):
        raise ParseError('기사 제목 또는 본문을 인식하지 못함')
    published = next((n.attrs['data-date-time'] for n in nodes if n.attrs.get('data-date-time')), None)
    modified = next((n.attrs['data-modify-date-time'] for n in nodes if n.attrs.get('data-modify-date-time')), None)
    publisher = meta.get('og:article:author')
    logo = next((n for n in nodes if 'media_end_head_top_logo' in n.attrs.get('class', '').split()), None)
    if logo:
        publisher = next((n.attrs.get('alt') for n in logo.nodes() if n.tag == 'img' and n.attrs.get('alt')), publisher)
    return {'url': canonical(url), 'title': title, 'publisher': publisher,
            'published_at': timestamp(published), 'modified_at': timestamp(modified),
            'time_unknown': timestamp(published) is None, 'body': tidy(body.text())}


def event_status(article, event):
    """최초 입력으로 창 배정. 사후 수정 본문은 당시 본문으로 쓰지 않는다."""
    if not article.get('published_at'):
        return 'time_unknown'
    published = datetime.fromisoformat(article['published_at'])
    start, end = (datetime.fromisoformat(event[k]) for k in ('news_window_start', 'news_window_end'))
    if not start < published <= end:
        return 'outside_window'
    modified = article.get('modified_at')
    if modified and datetime.fromisoformat(modified) > end:
        return 'modified_after_decision'
    return 'in_window'


def now():
    return datetime.now(KST).isoformat(timespec='seconds')


def connect(path):
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS searches (key TEXT PRIMARY KEY, status TEXT, payload TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS articles (url TEXT PRIMARY KEY, status TEXT, payload TEXT)')
    db.commit()
    return db


def save(db, table, key, status, payload):
    column = 'key' if table == 'searches' else 'url'
    db.execute(f'INSERT OR REPLACE INTO {table} ({column},status,payload) VALUES (?,?,?)',
               (key, status, json.dumps(payload, ensure_ascii=False)))
    db.commit()


class Fetcher:
    def __init__(self, directory, delay=3):
        self.directory, self.delay, self.last = directory, delay, 0
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'investment-evidence-research/0.1'

    def get(self, url):
        host = urlparse(url).hostname
        if host not in ('search.naver.com', 's.search.naver.com', 'n.news.naver.com'):
            raise ParseError('예상하지 않은 수집 호스트')
        time.sleep(max(0, self.delay - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        response = request_with_deadline(self.session, url, seconds=30)
        raw = self.directory / (hashlib.sha256(url.encode()).hexdigest() + '.txt')
        raw.write_bytes(response.content)
        response.raise_for_status()
        response.encoding = 'utf-8'
        return response.text, str(raw)


def request_with_deadline(session, url, seconds=30):
    """socket timeout만으로 제한되지 않는 DNS 대기까지 포함한 요청 기한."""
    result = queue.Queue(maxsize=1)
    cancelled = threading.Event()

    def work():
        try:
            response = session.get(url, timeout=seconds)
            if cancelled.is_set():
                response.close()
            else:
                result.put((response, None))
        except Exception as exc:
            if not cancelled.is_set():
                result.put((None, exc))

    # DNS 호출을 취소할 수 없어도 프로그램 종료를 막지 않도록 한다.
    threading.Thread(target=work, daemon=True).start()
    try:
        response, error = result.get(timeout=seconds)
    except queue.Empty:
        cancelled.set()
        raise requests.Timeout(f'요청 전체 대기시간 {seconds}초 초과: {url}') from None
    if error:
        raise error
    return response


def search_url(query, day):
    date = day.replace('-', '.')
    return 'https://search.naver.com/search.naver?' + urlencode({
        'where': 'news', 'query': query, 'pd': '3', 'ds': date, 'de': date, 'sort': '1', 'start': '1',
        'nso': f'so:dd,p:from{day.replace("-", "")}to{day.replace("-", "")},a:all'})


def search(fetch, query, day, max_pages):
    url = search_url(query, day)
    seen_urls, seen_results, records, raw_files = set(), set(), [], []
    for page in range(max_pages):
        if url in seen_urls:
            return 'incomplete', records, raw_files, '페이지 URL 반복'
        seen_urls.add(url)
        text, raw = fetch.get(url)
        raw_files.append(raw)
        if urlparse(url).hostname == 's.search.naver.com':
            payload = json.loads(text)
            if not isinstance(payload, dict) or not isinstance(payload.get('collection'), list) or 'url' not in payload:
                raise ParseError('페이지 넘김 응답 형식을 인식하지 못함')
            if any(not isinstance(c, dict) or not isinstance(c.get('html'), str) for c in payload['collection']):
                raise ParseError('페이지 넘김 목록 HTML 형식을 인식하지 못함')
            html = ''.join(c.get('html', '') for c in payload.get('collection', []))
            if not html.strip():
                if payload.get('url'):
                    raise ParseError('다음 URL은 있지만 목록 HTML이 없음')
                return ('complete' if records else 'empty'), records, raw_files, None
            batch, _, empty = parse_search(html)
            next_url = payload.get('url')
        else:
            batch, next_url, empty = parse_search(text)
        new = [r for r in batch if r['original_url'] not in seen_results]
        if batch and not new:
            return 'incomplete', records, raw_files, '반복 결과 페이지'
        records.extend(new)
        seen_results.update(r['original_url'] for r in new)
        if empty or not next_url:
            return ('complete' if records else 'empty'), records, raw_files, None
        url = next_url
    return 'incomplete', records, raw_files, '페이지 상한 도달'


def stock_filter(stock):
    """'all'이면 전체, 아니면 쉼표로 구분한 종목코드 집합."""
    return None if stock == 'all' else {s.strip() for s in stock.split(',') if s.strip()}


def extra_terms(path=TERMS_PATH):
    """search_terms.csv에 팀이 채운 약칭·영문명·종속회사. 그 이름 행의 사용 기간(from~to)에만 쓴다."""
    terms = {}
    if not Path(path).exists():
        return terms
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            words = [w.strip() for c in TERM_COLUMNS for w in (r.get(c) or '').split('|') if w.strip()]
            if words:
                terms.setdefault(r['stock_code'], []).append((r['from'], r['to'], words))
    return terms


def search_query(term, quote=True):
    """실제로 보내는 검색어. 따옴표로 감싸 '미코'가 '미코바이오메드'·'미코스' 등과 섞이는 것을 줄인다."""
    return f'"{term}"' if quote and not term.startswith('"') else term


def tasks(plan, args):
    wanted = stock_filter(args.stock)
    terms = extra_terms(getattr(args, 'terms', TERMS_PATH))
    with Path(plan).open(encoding='utf-8-sig', newline='') as f:
        rows = [r for r in csv.DictReader(f) if (wanted is None or r['stock_code'] in wanted)
                and args.start <= r['date'] <= args.end and int(r['priority']) <= args.max_priority]
    for row in sorted(rows, key=lambda r: (int(r['priority']), r['date'], r['stock_code'])):
        extra = [w for start, end, words in terms.get(row['stock_code'], []) if start <= row['date'] <= end for w in words]
        for q in dict.fromkeys([row['query']] + row['query_alt'].split('|') + extra):
            if q.strip():
                yield row, q.strip()


def export(db, directory, stocks, selected_keys=None):
    """전체 검색·기사 기록은 한 번만 쓰고, 사건 연결과 집계는 종목마다 쓴다."""
    searches = [json.loads(p) for (p,) in db.execute('SELECT payload FROM searches ORDER BY key')]
    articles = [json.loads(p) for (p,) in db.execute('SELECT payload FROM articles ORDER BY url')]
    for name, records in [('searches', searches), ('articles', articles)]:
        with (directory / f'{name}.jsonl').open('w', encoding='utf-8') as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
    with (HERE / 'output/drop_events.csv').open(encoding='utf-8-sig', newline='') as f:
        all_events = list(csv.DictReader(f))
    return {stock: export_stock(directory, stock, searches, articles,
                                [e for e in all_events if e['stock_code'] == stock], selected_keys)
            for stock in stocks}


def export_stock(directory, stock, searches, articles, events, selected_keys):
    links = []
    # selected_keys: 키 → 이번에 보낸 검색어. 따옴표 방식이 다른 예전 기록은 이번 범위에 세지 않는다
    relevant = [s for s in searches if s['stock_code'] == stock and (selected_keys is None or
                selected_keys.get(json.dumps([s['stock_code'], s['date'], s['query']], ensure_ascii=False))
                == s.get('search_query', s['query']))]
    urls = {r['naver_url'] for s in relevant for r in s.get('results', []) if r['naver_url']}
    for article in articles:
        if article.get('url') not in urls or article.get('status') != 'complete':
            continue
        for e in events:
            if not article.get('published_at'):
                dates = [s['date'] for s in relevant if any(r['naver_url'] == article['url'] for r in s.get('results', []))]
                if not any(e['news_window_start'][:10] <= d <= e['news_window_end'][:10] for d in dates):
                    continue
            status = event_status(article, e)
            if status != 'outside_window':
                links.append({'drop_id': e['drop_id'], 'url': article['url'], 'status': status})
    with (directory / f'event_links_{stock}.jsonl').open('w', encoding='utf-8') as f:
        for record in links:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    summary = {'stock_code': stock, 'search_statuses': {}, 'article_statuses': {}, 'event_statuses': {},
               'pending_articles': len(urls - {a['url'] for a in articles}),
               'external_only_results': sum(not r['naver_url'] for s in relevant for r in s.get('results', []))}
    for key, records in [('search_statuses', relevant), ('article_statuses', [a for a in articles if a.get('url') in urls]), ('event_statuses', links)]:
        for r in records:
            summary[key][r['status']] = summary[key].get(r['status'], 0) + 1
    (directory / f'summary_{stock}.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--stock', default='053660', help="종목코드, 쉼표 목록, 또는 all")
    ap.add_argument('--start', default='2018-12-14')
    ap.add_argument('--end', default='2018-12-19')
    ap.add_argument('--max-priority', type=int, choices=(1, 2, 3), default=1)
    ap.add_argument('--max-pages', type=int, default=30)
    ap.add_argument('--delay', type=float, default=3)
    ap.add_argument('--skip-automated', action='store_true', help='명확한 자동 시세 제목은 링크만 기록하고 본문 접속을 생략')
    ap.add_argument('--plan', type=Path, default=HERE / 'output/crawl_plan.csv')
    ap.add_argument('--terms', type=Path, default=TERMS_PATH, help='팀이 채운 추가 검색어 표')
    ap.add_argument('--no-quote', action='store_true', help='검색어를 따옴표 없이 보냄 (2026-11-04 파일럿·홀드아웃 방식)')
    ap.add_argument('--min-free-gb', type=float, default=5, help='남은 디스크가 이보다 적으면 멈춤 (응답 원문을 모두 저장한다)')
    ap.add_argument('--out', type=Path, default=HERE / 'cache/naver')
    args = ap.parse_args(argv)
    if args.delay < 1 or args.max_pages < 1 or args.start > args.end:
        ap.error('delay >= 1, max-pages >= 1, start <= end 필요')
    selected = list(tasks(args.plan, args))
    if not selected:
        ap.error('조건에 맞는 검색 대상이 없습니다')
    stocks = sorted({r['stock_code'] for r, _ in selected})
    print(f'[대상] {len(stocks)}종목, 검색 {len(selected)}번 (페이지 넘김 제외)', flush=True)
    args.out.mkdir(parents=True, exist_ok=True)
    db = connect(args.out / 'crawl.sqlite3')
    fetch = Fetcher(args.out, args.delay)
    selected_keys = {json.dumps([r['stock_code'], r['date'], q], ensure_ascii=False): search_query(q, not args.no_quote)
                     for r, q in selected}
    for row, query in selected:
        free_gb = shutil.disk_usage(args.out).free / 1e9
        if free_gb < args.min_free_gb:
            print(f'[중단] 남은 디스크 {free_gb:.1f}GB < {args.min_free_gb}GB', flush=True)
            break
        blocked = False
        sent = search_query(query, not args.no_quote)
        key = json.dumps([row['stock_code'], row['date'], query], ensure_ascii=False)
        old = db.execute('SELECT status,payload FROM searches WHERE key=?', (key,)).fetchone()
        data = {'stock_code': row['stock_code'], 'date': row['date'], 'query': query, 'search_query': sent,
                'priority': int(row['priority']), 'drop_ids': row['drop_ids'], 'collected_at': now()}
        # 같은 검색어라도 따옴표 여부가 다르면 결과가 달라서 재사용하지 않는다 (예전 기록은 search_query가 없다 = 따옴표 없음)
        if old and old[0] in ('complete', 'empty') and json.loads(old[1]).get('search_query', query) == sent:
            data = json.loads(old[1])
        else:
            try:
                status, results, raw_files, reason = search(fetch, sent, row['date'], args.max_pages)
                data.update(status=status, results=results, raw_files=raw_files, reason=reason)
            except (requests.RequestException, ParseError, ValueError) as exc:
                status = 'request_failed' if isinstance(exc, requests.RequestException) else 'parse_failed'
                data.update(status=status, error=str(exc), results=[])
            save(db, 'searches', key, data['status'], data)
        print(f"[검색] {row['date']} {row['stock_code']} {sent}: {data['status']} {len(data['results'])}건", flush=True)
        for result in data['results']:
            url = result['naver_url']
            if not url:
                continue
            old_article = db.execute('SELECT status FROM articles WHERE url=?', (url,)).fetchone()
            if old_article and (old_article[0] == 'complete' or (args.skip_automated and old_article[0] == 'excluded_automated')):
                continue
            if args.skip_automated:
                from news_review import automated_title
                if automated_title(result['title']):
                    save(db, 'articles', url, 'excluded_automated', {
                        'url': url, 'title': result['title'], 'status': 'excluded_automated',
                        'reason': '자동 시세 제목 형식: 본문 접속 생략', 'collected_at': now()})
                    continue
            article = {'url': url, 'collected_at': now()}
            try:
                html, raw = fetch.get(url)
                article.update(parse_article(html, url), raw_file=raw, status='complete')
            except (requests.RequestException, ParseError, ValueError) as exc:
                article.update(status='request_failed' if isinstance(exc, requests.RequestException) else 'parse_failed', error=str(exc))
                if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
                    blocked = True
                if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code in (403, 429):
                    blocked = True
            save(db, 'articles', url, article['status'], article)
            print(f"  [기사] {url}: {article['status']}", flush=True)
            if blocked:
                break
        if blocked or data['status'] in ('request_failed', 'parse_failed'):
            break  # 차단·접속 장애·구조 변경 시 대량 요청을 이어가지 않는다.
    summaries = export(db, args.out, stocks, selected_keys)
    db.close()
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    done = all(all(s in ('complete', 'empty') for s in m['search_statuses'])
               and set(m['article_statuses']) <= {'complete', 'excluded_automated'} and not m['pending_articles']
               for m in summaries.values())
    return 0 if done and len(selected_keys) == sum(sum(m['search_statuses'].values()) for m in summaries.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
