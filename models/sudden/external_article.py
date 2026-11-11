"""네이버에 수록되지 않은 언론사 기사(검색 결과의 외부 링크) 본문 수집.

큰 종목은 검색 결과 대부분이 외부 링크다(2026-11-09 알테오젠: 외부 442건, 네이버 수록 본문 13건, 사건 창 0건).
언론사마다 파서를 만들지 않고 범용 규칙으로 뽑는다. 외부 링크의 약 70%는 같은 CMS(articleView.html)다.

- 입력 시각: article:published_time 등 메타, 없으면 JSON-LD datePublished. 둘 다 없으면 time_unknown(분석에서 빠진다).
- 수정 시각: article:modified_time 등, JSON-LD dateModified. 없으면 사후 수정 여부를 모른다(네이버 수록 기사와 다른 한계).
- 본문: 알려진 본문 영역(itemprop=articleBody, article-view-content-div 등), 없으면 <p> 글이 가장 많은 영역.
- 언론사: og:site_name, 없으면 호스트 이름. P(언론사 확산)가 언론사를 모르면 계산을 보류하므로 비워 두지 않는다.
- robots.txt가 막으면 받지 않는다. 호스트마다 간격을 둔다.
"""
import json
import re
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

from naver_crawl import ParseError, Tree, request_with_deadline, tidy, timestamp

UA = 'investment-evidence-research/0.1'
BODY_IDS = ('article-view-content-div', 'articleBody', 'article_body', 'articlebody', 'article-body', 'articeBody',
            'newsEndContents', 'news_body_area', 'news-contents', 'news_content', 'newsContent', 'textBody',
            'article_content', 'article-content', 'view_content', 'viewContent', 'news-text', 'view-text', 'newsView')
PUBLISHED_META = ('article:published_time', 'og:article:published_time', 'dd:published_time', 'datePublished',
                  'pubdate', 'publish-date', 'date')
MODIFIED_META = ('article:modified_time', 'og:article:modified_time', 'dd:modified_time', 'dateModified')
MIN_BODY = 150          # 알려진 영역이 없을 때 <p> 글 최소 길이
MIN_KNOWN = 50          # 알려진 본문 영역의 최소 길이 (단신은 100자 남짓이다)


def decode(content, header_charset=None):
    """meta charset → 응답 헤더 → UTF-8 순. 국내 언론사는 아직 EUC-KR이 섞여 있다."""
    head = content[:4000].decode('ascii', errors='ignore')
    m = re.search(r'charset=["\']?([\w-]+)', head, re.I)
    for enc in (m and m[1], header_charset, 'utf-8', 'cp949'):
        if not enc:
            continue
        enc = 'cp949' if enc.lower() in ('euc-kr', 'ks_c_5601-1987', 'x-windows-949') else enc
        try:
            return content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode('utf-8', errors='replace')


def _ld_values(html, key):
    out = []
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if isinstance(x.get(key), str):
                    out.append(x[key])
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
    return out


def _first_time(meta, nodes, html, names, ld_key):
    for name in names:
        t = timestamp(meta.get(name))
        if t:
            return t
    for n in nodes:                                    # <time itemprop="datePublished" datetime=...> 등
        if n.attrs.get('itemprop') == ld_key:
            t = timestamp(n.attrs.get('datetime') or n.attrs.get('content'))
            if t:
                return t
    for v in _ld_values(html, ld_key):
        t = timestamp(v)
        if t:
            return t
    return None


def _body_node(nodes):
    for n in nodes:
        if n.attrs.get('itemprop') == 'articleBody' and len(tidy(n.text())) >= MIN_KNOWN:
            return n
    for name in BODY_IDS:
        for n in nodes:
            if n.attrs.get('id') == name or name in n.attrs.get('class', '').split():
                if len(tidy(n.text())) >= MIN_KNOWN:
                    return n
    best, size = None, 0                               # <p> 글이 가장 많은 영역
    for n in nodes:
        if n.tag not in ('div', 'article', 'section', 'td'):
            continue
        s = sum(len(tidy(c.text())) for c in n.children if getattr(c, 'tag', None) == 'p')
        if s > size:
            best, size = n, s
    return best if size >= MIN_BODY else None


def parse_external(html, url):
    nodes = list(Tree(html).root.nodes())
    meta = {}
    for n in nodes:
        if n.tag == 'meta':
            key = n.attrs.get('property') or n.attrs.get('name') or n.attrs.get('itemprop')
            if key and key not in meta:
                meta[key] = n.attrs.get('content')
    title = meta.get('og:title') or next((tidy(n.text()) for n in nodes if n.tag == 'title'), None)
    body = _body_node(nodes)
    if not title or body is None:
        raise ParseError('외부 기사 제목 또는 본문을 인식하지 못함')
    published = _first_time(meta, nodes, html, PUBLISHED_META, 'datePublished')
    modified = _first_time(meta, nodes, html, MODIFIED_META, 'dateModified')
    host = (urlparse(url).hostname or '').removeprefix('www.')
    return {'url': url, 'title': tidy(title), 'publisher': tidy(meta.get('og:site_name') or '') or host,
            'published_at': published, 'modified_at': modified, 'time_unknown': published is None,
            'modified_unknown': modified is None, 'source': 'external', 'body': tidy(body.text())}


class ExternalFetcher:
    """호스트마다 delay초 간격, robots.txt 확인. 응답 원문은 directory에 남긴다."""

    def __init__(self, directory, delay=3):
        self.directory, self.delay = directory, delay
        self.last, self.robots = {}, {}
        self.session = requests.Session()
        self.session.headers['User-Agent'] = UA

    def allowed(self, url):
        p = urlparse(url)
        host = p.hostname
        if host not in self.robots:
            rp = RobotFileParser()
            try:
                r = request_with_deadline(self.session, f'{p.scheme}://{p.netloc}/robots.txt', seconds=15)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])                               # robots.txt를 못 받으면 막지 않은 것으로 본다
            self.robots[host] = rp
        return self.robots[host].can_fetch(UA, url)

    def get(self, url):
        import hashlib
        host = urlparse(url).hostname
        if urlparse(url).scheme not in ('http', 'https') or not host:
            raise ParseError('외부 기사 주소 형식이 아님')
        if not self.allowed(url):
            raise PermissionError('robots.txt가 막음')
        time.sleep(max(0, self.delay - (time.monotonic() - self.last.get(host, 0))))
        self.last[host] = time.monotonic()
        response = request_with_deadline(self.session, url, seconds=30)
        raw = self.directory / (hashlib.sha256(url.encode()).hexdigest() + '.txt')
        raw.write_bytes(response.content)
        response.raise_for_status()
        return decode(response.content, response.encoding if 'charset' in response.headers.get('content-type', '') else None), str(raw)


def result_urls(results, articles):
    """분석에 쓰는 기사 주소: 네이버 수록 링크 + 본문을 받은 외부 링크.

    articles: {url: 기사 기록}. 외부 링크는 본문을 받은 것만 넣어서, 외부 수집을 하지 않은 예전 캐시의 결과가 바뀌지 않게 한다.
    """
    out = set()
    for r in results:
        if r.get('naver_url'):
            out.add(r['naver_url'])
        elif (a := articles.get(r.get('original_url'))) and a.get('status') == 'complete':
            out.add(r['original_url'])
    return out
