"""접속 없이 페이지 넘김·실패 구분·시간 경계·저장을 검증한다."""
import json
import sys
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import naver_crawl as n


def card(aid='123', nav=True):
    link = f'<a href="https://n.news.naver.com/mnews/article/018/{aid}?sid=101">네이버뉴스</a>' if nav else ''
    return f'<div><a data-heatmap-target=".tit" href="https://publisher.test/{aid}">현진소재 뉴스</a>{link}</div>'


def test_modern_results_keep_external_articles_and_next_url():
    url = 'https://s.search.naver.com/p/newssearch/4/api/tab/more?start=11&a=1'
    html = card() + card('124', False) + '<script>const options={"url":' + json.dumps(url).replace('&', r'\u0026') + '};</script>'
    rows, more, empty = n.parse_search(html)
    assert len(rows) == 2 and more == url and not empty
    assert rows[0]['naver_url'].endswith('/018/123')
    assert rows[1]['naver_url'] is None


def test_empty_and_unrecognized_response_are_different():
    assert n.parse_search('<p>검색결과가 없습니다.</p>') == ([], None, True)
    with pytest.raises(n.ParseError):
        n.parse_search('<h1>접속을 확인해 주세요</h1>')


def test_canonical_rejects_other_hosts_and_normalizes_old_links():
    assert n.canonical('https://news.naver.com/main/read.nhn?oid=018&aid=123') == n.canonical('https://n.news.naver.com/article/018/123?sid=101')
    assert n.canonical('https://n.news.naver.com.attacker.test/article/018/123') is None


def test_article_original_and_modified_times_are_separate():
    html = '<meta property="og:title" content="기사"><a class="media_end_head_top_logo"><img alt="언론사"></a><span data-date-time="2018-12-19 15:30:00"></span><span data-modify-date-time="2018-12-19 16:00:00"></span><article id="dic_area">본문<br>다음 문단<script>remove()</script></article>'
    article = n.parse_article(html, 'https://n.news.naver.com/article/018/123')
    assert article['published_at'] == '2018-12-19T15:30:00+09:00'
    assert article['modified_at'] == '2018-12-19T16:00:00+09:00'
    assert article['publisher'] == '언론사' and article['body'] == '본문 다음 문단'
    assert not article['time_unknown']
    missing = n.parse_article('<meta property="og:title" content="기사"><article id="dic_area">본문</article>', article['url'])
    assert missing['time_unknown'] and missing['published_at'] is None
    with pytest.raises(n.ParseError):
        n.parse_article('<meta property="og:title" content="삭제 기사">', article['url'])


def test_event_open_start_closed_end_and_late_edit():
    event = {'news_window_start': '2018-12-14T15:30:00+09:00', 'news_window_end': '2018-12-19T15:30:00+09:00'}
    assert n.event_status({}, event) == 'time_unknown'
    assert n.event_status({'published_at': event['news_window_start']}, event) == 'outside_window'
    assert n.event_status({'published_at': event['news_window_end']}, event) == 'in_window'
    assert n.event_status({'published_at': event['news_window_end'], 'modified_at': '2018-12-20T01:00:00+09:00'}, event) == 'modified_after_decision'


class FakeFetch:
    def __init__(self, responses):
        self.responses, self.urls = iter(responses), []

    def get(self, url):
        self.urls.append(url)
        return next(self.responses), 'raw.txt'


def test_infinite_scroll_and_explicit_end():
    url = 'https://s.search.naver.com/p/newssearch/4/api/tab/more?start=11'
    first = card() + '<script>{"url":' + json.dumps(url) + '}</script>'
    second = json.dumps({'collection': [{'html': card('124')}], 'url': None})
    fetch = FakeFetch([first, second])
    status, rows, _, _ = n.search(fetch, '현진소재', '2018-12-19', 3)
    assert status == 'complete' and len(rows) == 2 and fetch.urls[-1] == url
    status, _, _, reason = n.search(FakeFetch([first]), '현진소재', '2018-12-19', 1)
    assert status == 'incomplete' and reason == '페이지 상한 도달'


def test_repeated_page_is_incomplete_not_complete():
    url = 'https://s.search.naver.com/p/newssearch/4/api/tab/more?start=11'
    first = card() + '<script>{"url":' + json.dumps(url) + '}</script>'
    second = json.dumps({'collection': [{'html': card()}], 'url': None})
    assert n.search(FakeFetch([first, second]), '현진소재', '2018-12-19', 3)[0] == 'incomplete'


def test_unrecognized_pagination_json_is_not_treated_as_end():
    url = 'https://s.search.naver.com/p/newssearch/4/api/tab/more?start=11'
    first = card() + '<script>{"url":' + json.dumps(url) + '}</script>'
    with pytest.raises(n.ParseError):
        n.search(FakeFetch([first, '{}']), '현진소재', '2018-12-19', 3)


def test_progress_upsert_preserves_one_article():
    db = n.connect(':memory:')
    n.save(db, 'articles', 'url', 'request_failed', {'error': 'timeout'})
    n.save(db, 'articles', 'url', 'complete', {'title': '기사'})
    assert db.execute('SELECT count(*),status FROM articles').fetchone() == (1, 'complete')
    db.close()


def test_request_deadline_bounds_stuck_dns_like_call():
    release = threading.Event()
    closed = threading.Event()
    class Response:
        def close(self):
            closed.set()
    class Session:
        def get(self, *args, **kwargs):
            release.wait(2)
            return Response()
    try:
        with pytest.raises(requests.Timeout, match='전체 대기시간'):
            n.request_with_deadline(Session(), 'https://search.naver.com', seconds=0.02)
    finally:
        release.set()
    assert closed.wait(1)


def test_request_deadline_preserves_transport_error():
    class Session:
        def get(self, *args, **kwargs):
            raise requests.ConnectionError('DNS error')
    with pytest.raises(requests.ConnectionError, match='DNS error'):
        n.request_with_deadline(Session(), 'https://search.naver.com', seconds=1)


def test_request_deadline_returns_successful_response():
    response = object()
    class Session:
        def get(self, *args, **kwargs):
            return response
    assert n.request_with_deadline(Session(), 'https://search.naver.com', seconds=1) is response


def write_plan(path, rows):
    path.write_text('stock_code,date,weekday,query,query_alt,priority,priority_name,drop_ids\n'
                    + ''.join(f'{r}\n' for r in rows), encoding='utf-8-sig')


def test_tasks_mix_stocks_by_priority_and_add_team_terms(tmp_path):
    plan, terms = tmp_path / 'plan.csv', tmp_path / 'terms.csv'
    write_plan(plan, ['059090,2018-01-02,화,미코,,3,n_base,', '068790,2018-01-03,수,DMS,,1,event_window,x',
                      '059090,2018-01-03,수,미코,,1,event_window,y', '097800,2018-01-03,수,윈팩,,1,event_window,z'])
    terms.write_text('stock_code,company_name,from,to,약칭,영문명,주요_종속회사,제외_접두어\n'
                     '068790,DMS,2015-01-02,2018-01-02,,디엠에스,,\n'
                     '068790,DMS,2018-01-03,2019-12-30,,디엠에스|DMS Co,,\n', encoding='utf-8-sig')
    args = SimpleNamespace(stock='059090,068790', start='2018-01-01', end='2018-12-31', max_priority=3, terms=terms)
    got = [(r['stock_code'], r['date'], q) for r, q in n.tasks(plan, args)]
    assert got == [('059090', '2018-01-03', '미코'), ('068790', '2018-01-03', 'DMS'),
                   ('068790', '2018-01-03', '디엠에스'), ('068790', '2018-01-03', 'DMS Co'),
                   ('059090', '2018-01-02', '미코')]
    args.stock = 'all'
    assert {r['stock_code'] for r, _ in n.tasks(plan, args)} == {'059090', '068790', '097800'}


def test_search_query_quotes_once():
    assert n.search_query('미코') == '"미코"'
    assert n.search_query('"미코"') == '"미코"'
    assert n.search_query('미코', quote=False) == '미코'


def test_main_does_not_reuse_unquoted_record_for_quoted_search(tmp_path, monkeypatch):
    plan, out = tmp_path / 'plan.csv', tmp_path / 'out'
    write_plan(plan, ['053660,2018-12-14,금,현진소재,,1,event_window,'])
    out.mkdir()
    db = n.connect(out / 'crawl.sqlite3')
    key = json.dumps(['053660', '2018-12-14', '현진소재'], ensure_ascii=False)
    n.save(db, 'searches', key, 'empty', {'stock_code': '053660', 'date': '2018-12-14', 'query': '현진소재',
                                          'status': 'empty', 'results': []})
    db.close()
    sent = []
    monkeypatch.setattr(n, 'search', lambda fetch, q, day, pages: sent.append(q) or ('empty', [], [], None))
    argv = ['--stock', 'all', '--start', '2018-12-14', '--end', '2018-12-14', '--plan', str(plan),
            '--terms', str(tmp_path / 'none.csv'), '--out', str(out), '--min-free-gb', '0']
    assert n.main(argv + ['--no-quote']) == 0 and sent == []        # 예전 방식은 기록 재사용
    assert n.main(argv) == 0 and sent == ['"현진소재"']              # 따옴표 방식은 새로 검색
    assert n.main(argv) == 0 and sent == ['"현진소재"']              # 그 뒤로는 재사용
    assert (out / 'summary_053660.json').exists()
