"""외부 언론사 기사 범용 추출: 합성 HTML로 확인한다 (실제 기사 문장은 넣지 않는다)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import pytest

import external_article as ea
from naver_crawl import ParseError

BODY = '가나다라마바사 ' * 40


def page(head='', body_attr='id="article-view-content-div"', body=BODY, extra=''):
    return (f'<html><head><meta charset="utf-8"><meta property="og:title" content="테스트전자 3분기 실적">{head}</head>'
            f'<body><div class="menu"><p>메뉴</p></div><article {body_attr}><p>{body}</p><script>var x=1;</script></article>{extra}</body></html>')


def test_cms_meta_time_and_body():
    html = page('<meta property="og:site_name" content="테스트일보">'
                '<meta property="article:published_time" content="2026-11-09T10:20:00+09:00">'
                '<meta property="article:modified_time" content="2026-11-09T11:00:00+09:00">')
    a = ea.parse_external(html, 'https://www.test.co.kr/news/articleView.html?idxno=1')
    assert a['publisher'] == '테스트일보' and a['published_at'] == '2026-11-09T10:20:00+09:00'
    assert a['modified_at'] == '2026-11-09T11:00:00+09:00' and not a['time_unknown'] and a['source'] == 'external'
    assert a['body'].startswith('가나다라') and 'var x' not in a['body'] and '메뉴' not in a['body']


def test_json_ld_time_host_publisher_and_p_fallback():
    ld = '<script type="application/ld+json">{"@graph":[{"@type":"NewsArticle","datePublished":"2026-11-09 14:05:00"}]}</script>'
    html = page(ld, body_attr='class="something"')          # 알려진 본문 영역이 없으면 <p>가 가장 많은 영역
    a = ea.parse_external(html, 'https://www.example.kr/view.php?no=3')
    assert a['published_at'] == '2026-11-09T14:05:00+09:00'      # 시간대가 없으면 한국 시각
    assert a['publisher'] == 'example.kr' and a['modified_unknown'] is True


def test_time_unknown_and_unparseable():
    a = ea.parse_external(page(), 'https://a.kr/x')
    assert a['time_unknown'] is True and a['published_at'] is None
    with pytest.raises(ParseError):
        ea.parse_external(page(body='짧음', body_attr='class="x"'), 'https://a.kr/x')


def test_decode_euc_kr():
    raw = '<meta charset="euc-kr"><p>한글 본문</p>'.encode('cp949')
    assert '한글 본문' in ea.decode(raw)


def test_result_urls_only_adds_completed_external():
    results = [{'naver_url': 'https://n.news.naver.com/mnews/article/001/1', 'original_url': 'https://x.kr/1'},
               {'naver_url': None, 'original_url': 'https://ext.kr/2'},
               {'naver_url': None, 'original_url': 'https://ext.kr/3'},
               {'naver_url': None, 'original_url': 'https://ext.kr/4'}]
    articles = {'https://ext.kr/2': {'status': 'complete'}, 'https://ext.kr/3': {'status': 'external_failed'}}
    assert ea.result_urls(results, articles) == {'https://n.news.naver.com/mnews/article/001/1', 'https://ext.kr/2'}
    assert ea.result_urls(results, {}) == {'https://n.news.naver.com/mnews/article/001/1'}   # 예전 캐시는 그대로


def test_empty_known_region_falls_back():
    html = page(body_attr='itemprop="articleBody"', body='', extra=f'<div class="x"><p>{BODY}</p></div>')
    assert ea.parse_external(html, 'https://a.kr/x')['body'].startswith('가나다라')
