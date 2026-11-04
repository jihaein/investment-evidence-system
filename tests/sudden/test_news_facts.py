"""역할·거래 수치·배경·거래 상태를 혼동하지 않는지 확인한다."""
import sys
import json
import sqlite3
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import news_facts as f
import news_review as r
import np_scores as n

EVENT = {'stock_code': '053660', 'n_base_start': '2018-06-17T15:30:00+09:00',
         'p_base_start': '2018-09-15T15:30:00+09:00', 'base_end': '2018-12-14T15:30:00+09:00',
         'news_window_end': '2018-12-19T15:30:00+09:00'}
CONFIG = {'news_representation':'facts-v1', 'p_observation_max_hours':72,
          'small_baseline_warning_below':20, 'group_similarity_cut':0.42}


def article(url='a', title='현진소재, 200억 CB 발행', body='현진소재는 한스인베스트먼트를 대상으로 200억원 전환사채를 발행키로 결정했다.', date='2018-12-17T10:00:00+09:00', publisher='A'):
    return {'url':url,'title':title,'body':body,'published_at':date,'modified_at':None,'publisher':publisher,'status':'complete'}


def score(articles):
    rows,_ = r.prepare(articles, EVENT, ['현진소재'])
    return n.score({'drop_id':'test','event':EVENT,'coverage':{},'ready_for_baseline_analysis':True},rows,CONFIG)


def test_roles_are_not_reversed_when_stock_is_acquisition_target():
    a = article(title='인트로메딕, 현진소재 주식 34억원에 취득', body='인트로메딕은 현진소재의 주식 150만주를 34억1250만원에 취득한다. 지분비율은 10.67%다.')
    fact = f.extract(a)['primary']
    assert (fact['actor'],fact['target']) == ('인트로메딕','현진소재')
    assert fact['fields']['amount_krw'] == '3412500000'
    assert fact['fields']['shares'] == '1500000'


@pytest.mark.parametrize('raw,value',[('7천800','7800'),('150만','1500000'),('34억1250만','3412500000'),('270억','27000000000')])
def test_korean_numbers_are_normalized(raw,value):
    assert f.number(raw) == value


def test_background_market_size_is_not_acquisition_price():
    a=article(title='현진소재, 광산 투자로 신사업 추진', body='[매장량 4~5조원 추정] 현진소재는 전문회사 지분 인수에 나선다. 무한광업 주식 7800주를 취득했다. 주식 취득금액은 270억원이다. 지분율은 15%다.')
    fact=f.extract(a)['primary']
    assert fact['target']=='무한광업' and fact['fields']['amount_krw']=='27000000000'


def test_office_open_is_primary_and_old_mou_is_context():
    a=article(title='현진소재, 관계사 서울사무소 개소', body='현진소재의 관계사 무한광업이 지난 13일 서울사무소를 개소했다. 한편 지난달 무한광업과 금 유통 협약을 체결한 바 있다.')
    x=f.extract(a)
    assert (x['primary']['actor'],x['primary']['action'],x['primary']['target'])==('무한광업','office_open','서울사무소')
    assert x['primary']['fields']['event_date']=='2018-12-13'
    assert x['context']==['gold_mining_or_distribution']


def test_same_amount_different_action_and_counterparty_stay_separate():
    issue=article()
    other_party=article('b',body='현진소재는 다른투자사를 대상으로 200억원 전환사채를 발행한다.')
    acquire=article('c',title='현진소재, 다른회사 지분 200억원에 취득',body='현진소재는 다른회사 주식 100만주를 200억원에 취득한다.')
    assert len(f.cluster([issue,other_party,acquire]))==3


def test_perpetual_and_ordinary_cb_are_different_facts():
    ordinary=article()
    perpetual=article('b',title='현진소재, 200억 영구 CB 발행')
    assert len(f.cluster([ordinary,perpetual]))==2


def test_amount_change_is_not_hidden_by_similar_words():
    assert len(f.cluster([article(),article('b',title='현진소재, 270억 CB 발행',body='현진소재는 한스인베스트먼트를 대상으로 270억원 전환사채 발행을 결정했다.')]))==2


def test_missing_details_cannot_bridge_conflicting_transactions():
    a=article()
    b=article('b',body='현진소재는 다른투자사를 대상으로 200억원 전환사채 발행을 결정했다.')
    vague=article('c',body='현진소재는 200억원 전환사채를 발행하기로 했다.')
    groups=f.cluster([a,b,vague])
    assert len(groups)==3 and sum(g.get('grouping_method')=='lexical_fallback' for g in groups)==1


def test_cancelled_and_completed_do_not_merge_with_announcement():
    rows=[article(),article('b',title='현진소재, 200억 CB 발행 취소'),article('c',title='현진소재, 200억 CB 발행 완료')]
    assert len(f.cluster(rows))==3


def test_denial_is_held_instead_of_treated_as_issuance():
    assert f.extract(article(title='현진소재, CB 발행 보도 사실무근'))['primary'] is None


def test_instrument_mention_and_subscription_update_are_not_new_issuance():
    assert f.extract(article(title='현진소재, 전환사채 만기 연장'))['primary'] is None
    assert f.extract(article(title='현진소재, 440억 유상증자 청약률 공시'))['primary'] is None


def test_repeated_primary_fact_scores_zero_and_unparsed_news_is_held():
    old=article('old',date='2018-10-01T10:00:00+09:00')
    current=article()
    unknown=article('unknown',title='현진소재 업황 회복 기대',body='현진소재는 업황 회복을 기대한다고 밝혔다.')
    result=score([old,current,unknown])
    valid=[g for g in result['groups'] if g.get('primary_fact')]
    held=[g for g in result['groups'] if not g.get('primary_fact')]
    assert valid[0]['N']['value']==0 and valid[0]['N']['scope']=='primary_fact_only'
    assert held[0]['N']['value'] is None and held[0]['P']['value'] is None
    assert result['status']=='partial'


def test_unknown_prior_details_are_not_counted_as_new():
    old=article('old',body='현진소재는 200억원 전환사채 발행을 결정했다.',date='2018-10-01T10:00:00+09:00')
    result=score([old,article()])['groups'][0]['N']
    assert result['value']==0 and result['unverified_details']==['counterparty']


def test_new_office_action_in_known_business_context_is_explicit():
    old=article('old',title='현진소재, 무한광업과 금 유통 협약 체결',body='현진소재는 무한광업과 금 유통 협약을 체결했다.',date='2018-10-01T10:00:00+09:00')
    new=article(title='현진소재, 관계사 서울사무소 개소',body='현진소재는 관계사 무한광업이 지난 13일 서울사무소를 개소했다고 밝혔다. 금광 사업을 추진한다.')
    result=score([old,new])['groups'][0]['N']
    assert result['value']==1 and result['known_context']==['gold_mining_or_distribution']


def test_small_fact_reference_pool_holds_p_and_keeps_diagnostic_only():
    result=score([article('old',date='2018-10-01T10:00:00+09:00'),article()])['groups'][0]['P']
    assert result['status']=='held' and result['value'] is None
    assert result['diagnostic_percentile']==0.5 and result['hold_reason']=='fact_reference_sample_too_small'


def test_future_article_does_not_change_fact_metrics():
    base=[article('old',date='2018-10-01T10:00:00+09:00'),article()]
    assert score(base)==score(base+[article('future',date='2018-12-20T10:00:00+09:00',publisher='future')])


def test_equivalent_acquisition_words_and_number_formats_share_a_fact():
    a=article(title='현진소재, 개발회사 지분 15% 양수',body='현진소재는 개발회사 주식 7천800주를 270억원에 양수한다. 지분율은 15%다.')
    b=article('b',title='현진소재, 개발회사 지분 인수',body='현진소재는 개발회사 주식 7800주를 270억 원에 취득한다. 지분율은 15%다.',publisher='B')
    assert len(f.cluster([a,b],cutoff=0.99))==1


def test_different_office_action_dates_do_not_merge():
    a=article(title='현진소재, 관계사 서울사무소 개소',body='관계사 무한광업이 지난 13일 서울사무소를 개소했다.')
    b=article('b',title=a['title'],body='관계사 무한광업이 지난 14일 서울사무소를 개소했다.')
    assert len(f.cluster([a,b]))==2


def test_cached_articles_match_manually_reviewed_primary_fact_groups():
    root=Path(__file__).resolve().parents[2]
    db_path=root/'models/sudden/cache/naver/crawl.sqlite3'
    if not db_path.exists():
        pytest.skip('실제 기사 캐시가 없는 환경')
    cases=json.loads((Path(__file__).parent/'fixtures/fact_review_cases.json').read_text(encoding='utf-8'))['cases']
    with sqlite3.connect(db_path) as db:
        source={url:json.loads(payload) for url,payload in db.execute('SELECT url,payload FROM articles WHERE status="complete"')}
    prefix='https://n.news.naver.com/mnews/article/'
    for case in cases:
        ids=[i for g in case['groups'] for i in g['articles']]+case['needs_review']
        if any(prefix+i not in source for i in ids):
            pytest.skip('회귀 사례 기사 일부가 캐시에 없음')
        groups=f.cluster([source[prefix+i] for i in ids],cutoff=0.5)
        actual=[g for g in groups if g.get('primary_fact')]
        assert len(actual)==len(case['groups'])
        for expected in case['groups']:
            group=next(g for g in actual if {m['url'] for m in g['members']}=={prefix+i for i in expected['articles']})
            for key in ('actor','action','target'):
                assert group['primary_fact'][key]==expected[key]
            if 'amount_krw' in expected:
                assert group['primary_fact']['fields']['amount_krw']==expected['amount_krw']
        assert all(f.extract(source[prefix+i])['primary'] is None for i in case['needs_review'])
