"""주가 반응을 원인 기사로 세지 않고, 미래 기사 없이 묶음을 만드는지 검증."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import news_review as r

EVENT = {'stock_code': '053660', 'n_base_start': '2018-06-17T15:30:00+09:00',
         'p_base_start': '2018-09-15T15:30:00+09:00', 'base_end': '2018-12-14T15:30:00+09:00',
         'news_window_end': '2018-12-19T15:30:00+09:00'}


def article(url='a', time='2018-12-17T14:00:00+09:00', title='현진소재, 사무소 개소', body=None, publisher='A'):
    return {'url': url, 'published_at': time, 'modified_at': None, 'title': title,
            'body': body or '현진소재는 관계사 무한광업 서울사무소를 개소하고 금왕광산의 금 채굴을 준비한다고 밝혔다.',
            'publisher': publisher, 'status': 'complete'}


def test_company_fact_price_reaction_and_roundup_are_separate():
    assert r.classify(article(), '053660', ['현진소재'])['kind'] == 'company_news'
    assert r.classify(article(title='현진소재, 하한가 진입'), '053660', ['현진소재'])['kind'] == 'price_reaction'
    assert r.classify(article(title='현진소재, 주가급락 관련 조회공시 요구'), '053660', ['현진소재'])['kind'] == 'company_news'
    assert r.classify(article(title='오전 코스닥 상승세'), '053660', ['현진소재'])['kind'] == 'roundup'
    unrelated = r.classify(article(title='타사 소식', body='다른 기업에 관한 소식'), '053660', ['현진소재'])
    assert unrelated['kind'] == 'unrelated' and not unrelated['candidate_for_content_analysis']
    assert r.classify(article(title='관계사 소식'), '053660', ['현진소재'])['kind'] == 'mention_review'


def test_boundaries_do_not_double_count_event_and_baseline():
    assert r.period_of(article(time=EVENT['base_end']), EVENT) == ('p_base', 'eligible_by_timestamp')
    assert r.period_of(article(time=EVENT['n_base_start']), EVENT) == ('n_base_extra', 'eligible_by_timestamp')
    assert r.period_of(article(time=EVENT['news_window_end']), EVENT) == ('event_window', 'eligible_by_timestamp')
    assert r.period_of(article(time='2018-12-19T15:30:01+09:00'), EVENT)[0] is None
    assert r.period_of({**article(), 'published_at': None}, EVENT)[1] == 'time_unknown'


def test_modified_after_baseline_cutoff_is_excluded_from_baseline():
    a = article(time='2018-12-01T10:00:00+09:00')
    a['modified_at'] = '2018-12-17T10:00:00+09:00'
    rows, data = r.prepare([a], EVENT, ['현진소재'])
    assert rows[0]['temporal_status'] == 'modified_after_cutoff'
    assert data['n_base']['article_count'] == 0


def test_similar_reports_keep_publishers_but_one_group():
    first = article()
    second = article(url='b', publisher='B', time='2018-12-17T15:00:00+09:00')
    groups = r.cluster([second, first, {**second, 'url': 'c'}])
    assert len(groups) == 1 and groups[0]['article_count'] == 3
    assert groups[0]['publisher_count'] == 2 and groups[0]['representative_url'] == 'a'


def test_different_facts_and_repeated_news_after_three_days_stay_separate():
    rows = [article(), article(url='b', body='풍력 발전 기업의 주식 취득을 위해 사모 전환사채를 발행한다는 공시를 발표했다.'),
            article(url='c', time='2018-12-21T15:00:00+09:00')]
    assert len(r.cluster(rows)) == 3


def test_future_article_cannot_become_representative_or_increase_publisher_count():
    before = article()
    after = article(url='future', time='2018-12-19T17:00:00+09:00', publisher='future')
    _, early = r.prepare([before], EVENT, ['현진소재'])
    _, late = r.prepare([before, after], EVENT, ['현진소재'])
    assert early == late
    assert early['event_window']['groups'][0]['publishers'] == ['A']


def test_n_base_includes_p_base_without_event_articles():
    _, data = r.prepare([article(url='n', time='2018-07-01T10:00:00+09:00'),
                         article(url='p', time='2018-10-01T10:00:00+09:00'), article()], EVENT, ['현진소재'])
    assert [data[k]['article_count'] for k in ('event_window', 'p_base', 'n_base')] == [1, 1, 2]


def test_boilerplate_does_not_create_duplicate_group():
    boiler = ' ▶ 구독하세요 무단전재 금지' * 100
    groups = r.cluster([article(body='현진소재는 새로운 광산개발 사무소를 개소했다.'+boiler),
                        article(url='b', body='전환사채 발행과 풍력발전업체 지분 인수를 공시했다.'+boiler)])
    assert len(groups) == 2


def test_only_known_automated_titles_can_skip_body_fetch():
    assert r.automated_title('<코>현진소재, 하한가 진입')
    assert r.automated_title('오전 10:30 현재 코스닥은 상승세')
    assert not r.automated_title('현진소재 급락, 무슨 일이 있었나')
    assert not r.automated_title('[특징주]현진소재, 금광 계약 기대감에 강세')
    assert not r.automated_title('<코>현진소재, 계약 공시')


def test_search_coverage_requires_aliases_and_does_not_count_failure_as_empty(monkeypatch):
    import naver_crawl
    monkeypatch.setattr(naver_crawl, 'tasks', lambda *args: [({'date': '2018-12-14'}, '현진소재'),
                                                          ({'date': '2018-12-14'}, '다른이름')])
    first = {'stock_code': '053660', 'date': '2018-12-14', 'query': '현진소재', 'status': 'empty', 'results': []}
    cover = r.coverage([first], EVENT)
    assert not cover['search_coverage_complete'] and len(cover['missing_searches']) == 1
    failure = {**first, 'query': '다른이름', 'status': 'request_failed'}
    assert not r.coverage([first, failure], EVENT)['search_coverage_complete']
    done = {**failure, 'status': 'complete'}
    assert r.coverage([first, done], EVENT)['search_coverage_complete']


def test_changed_amount_is_not_merged_even_with_identical_surrounding_text():
    body = '현진소재는 운영자금 조달을 위해 {}억원 규모 전환사채를 발행한다고 공시했다. 회사는 사업을 안정적으로 추진하고 지속적인 성장을 위해 자금을 조달한다.'
    assert len(r.cluster([article(body=body.format(43)), article(url='b', body=body.format(50))])) == 2
    assert r.quantities('43억원 2만주') == r.quantities('4,300,000,000원 20,000주')
