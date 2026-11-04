"""같은 공급계약과 다른 발주처·금액·기간·통화를 혼동하지 않는지 검증."""
import sys
import json
import sqlite3
from copy import deepcopy
import pytest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'models/sudden'))
import news_facts as f


def article(url='a',money='100억원',party='가온테크',end='2019년 12월 31일',product='반도체 제조장비'):
    return {'url':url,'title':f'새기업, {party}와 {money} 규모 {product} 공급계약',
            'body':f'새기업은 {party}와 {product} 공급계약을 체결했다고 25일 공시했다. 계약금액은 {money}이다. 계약기간은 2018년 5월 25일부터 {end}까지다.',
            'published_at':'2018-05-25T10:00:00+09:00','publisher':'신문A','modified_at':None,'status':'complete'}


def test_contract_fields_and_evidence():
    x=f.extract(article())['primary']
    assert x['action']=='supply_contract' and x['actor']=='새기업' and x['target']=='반도체제조장비'
    assert x['fields']['counterparty']=='가온테크'
    assert x['fields']['amount_krw']=='10000000000' and x['fields']['currency']=='KRW'
    assert x['fields']['contract_start']=='2018-05-25' and x['fields']['contract_end']=='2019-12-31'
    assert '100억원' in x['evidence']['amount_krw']


@pytest.mark.parametrize('party',['BK INTERNATIONAL.CO.,LTD','ACME Industrial Inc.','한산'])
def test_explicit_party_preserves_complete_legal_name(party):
    a=article()
    a['body']+=f' 계약상대방 : {party} -. 판매 및 공급지역 : 유럽, 아시아 -. 기타 : 해당 없음'
    fact=f.extract(a)['primary']
    assert fact['fields']['counterparty']==party
    assert party in fact['evidence']['counterparty']
    assert fact['fields']['delivery_region']=='유럽, 아시아'


@pytest.mark.parametrize('name,expected',[
    ('장비회사(Global Equipment Inc.)','장비회사'),
    ('ACME Industrial Inc.','ACME Industrial Inc.'),
    ('BK INTERNATIONAL.CO.,LTD','BK INTERNATIONAL.CO.,LTD'),
])
def test_party_in_contract_sentence_accepts_parenthetical_and_english_names(name,expected):
    a=article()
    a['body']=f'새기업은 미국 {name}와 반도체 장비 공급계약을 체결했다. 계약금액은 100억원이다. 계약기간은 2019년 12월31일까지다.'
    fact=f.extract(a)['primary']
    assert fact['fields']['counterparty']==expected
    assert 'delivery_region' not in fact['fields']


def test_supplier_or_buyer_country_does_not_override_delivery_destination():
    a=article()
    a['body']='새기업은 미국 장비회사(Global Equipment Inc.)와 반도체 장비 공급계약을 체결했다. 계약금액은 100억원이다. 장비회사의 미주 및 해외 각 공장에 필요한 물량을 공급한다.'
    fact=f.extract(a)['primary']
    assert fact['fields']['counterparty']=='장비회사'
    assert fact['fields']['delivery_region']=='미주 및 해외'


def test_country_alone_is_not_a_contract_counterparty():
    a=article()
    a['body']='새기업은 미국과 교역 중이다. 반도체 장비 공급계약을 체결했다. 계약금액은 100억원이다. 계약기간은 2019년 12월31일까지다.'
    fact=f.extract(a)['primary']
    assert 'counterparty' not in fact['fields'] and 'delivery_region' not in fact['fields']


@pytest.mark.parametrize('value',['업체','회사','중국 업체','중국업체','중국의 현지 업체','제조업체',
                                 '해당 회사','발주사','고객사','비공개','미공개'])
def test_unnamed_party_is_absent_in_both_heading_and_contract_sentence(value):
    for body in [f'계약상대방 : {value} -. 계약금액 : 100억원',
                 f'새기업은 {value}와 반도체 제조장비 공급계약을 체결했다.']:
        assert f.contract_party(body)==(None,None)


@pytest.mark.parametrize('value',['가온테크','한산','미래회사','중국중차','업체A','ACME Equipment Ltd.'])
def test_generic_party_filter_does_not_remove_company_names(value):
    assert not f.unnamed_contract_party(value)
    assert f.contract_party(f'계약상대방 : {value} -. 계약금액 : 100억원')[0]==value


def test_explicit_anonymous_party_does_not_fall_back_to_other_body_company():
    body='계약상대방 : 비공개 -. 참고 : 최종수요사와 반도체 제조장비 공급계약을 협의했다.'
    assert f.contract_party(body)==(None,None)


def test_missing_named_party_can_still_preserve_contract_with_explicit_period():
    a=article()
    a['body']='새기업은 중국 업체와 100억원 규모 반도체 제조장비 공급계약을 체결했다. 계약기간은 2019년 12월31일까지다.'
    fact=f.extract(a)['primary']
    assert fact['fields']['amount_krw']=='10000000000'
    assert fact['fields']['contract_end']=='2019-12-31'
    assert 'counterparty' not in fact['fields'] and 'counterparty' not in fact['evidence']


def test_region_does_not_cross_sentence_into_later_supply_context():
    assert f.contract_region('미국에 본사가 있다. 올해 제품을 공급할 계획이다.')==(None,None)
    assert f.contract_region('미국에 본사를 둔 회사와 제품 공급계약을 체결했다.')==(None,None)


def test_narrative_party_stops_at_copula_before_other_contract_fields():
    party,evidence=f.contract_party('계약 상대방은 (주)가온테크이고, 계약금액은 100억원이다.')
    assert party=='가온테크' and '100억원' not in evidence


def test_region_preserves_all_destinations_in_explicit_list():
    region,evidence=f.contract_region('판매·공급지역은 미주 및 아시아, 유럽 등으로 예상된다.')
    assert region=='미주, 아시아, 유럽' and '미주 및 아시아, 유럽' in evidence


def test_explicit_region_supports_locations_outside_context_vocabulary():
    assert f.contract_region('공급지역은 폴란드이다. 계약금액은 100억원이다.')[0]=='폴란드'


def test_relative_year_is_resolved_from_publication_not_current_year():
    x=f.extract(article(end='내년 12월31일'))['primary']
    assert x['fields']['contract_end']=='2019-12-31'


@pytest.mark.parametrize('period,start,end',[
    ('11월5일까지',None,'2018-11-05'),
    ('지난 8일부터 2020년 6월17일까지','2018-06-08','2020-06-17'),
    ('지난 25일부터 2020년 6월17일까지','2018-05-25','2020-06-17'),
    ('올해 11월5일까지',None,'2018-11-05'),
    ('내년 2월1일까지',None,'2019-02-01'),
    ('2017-03-27 ~ 2018-11-05','2017-03-27','2018-11-05'),
])
def test_contract_dates_use_publication_anchor_and_preserve_relative_evidence(period,start,end):
    from datetime import datetime
    fields,evidence=f.contract_period('계약기간은 '+period+'다.',datetime.fromisoformat('2018-06-11T10:00:00+09:00'))
    assert fields['contract_end']==end
    assert fields.get('contract_start')==start
    assert all(value in period for value in evidence.values())


def test_previous_day_expression_handles_year_boundary():
    from datetime import datetime
    fields,_=f.contract_period('계약기간은 지난 28일부터 2020년 6월17일까지다.',datetime.fromisoformat('2018-01-04T10:00:00+09:00'))
    assert fields['contract_start']=='2017-12-28'


def test_omitted_year_end_is_not_silently_rolled_into_next_year():
    a=article(end='1월5일')
    assert f.extract(a)['reason']=='invalid_contract_period'


def test_missing_day_does_not_get_filled_from_a_different_sentence():
    a=article()
    a['body']='새기업은 가온테크와 100억원 규모 반도체 제조장비 공급계약을 체결했다. 계약기간은 2020년 7월까지다. 다른 일정은 8월5일이다.'
    fact=f.extract(a)['primary']
    assert 'contract_end' not in fact['fields']


def test_contract_end_date_label_is_supported():
    from datetime import datetime
    fields,evidence=f.contract_period('계약 종료일은 11월5일이다.',datetime.fromisoformat('2018-03-28T10:00:00+09:00'))
    assert fields=={'contract_end':'2018-11-05'}
    assert evidence=={'contract_end':'11월5일'}


def test_primary_amount_does_not_use_revenue_or_market_size():
    a=article()
    a['body']='신규 시장은 5조원 규모다. '+a['body']+' 최근 매출액은 427억원이다.'
    assert f.extract(a)['primary']['fields']['amount_krw']=='10000000000'


def test_end_user_is_not_invented_as_contract_counterparty():
    a=article()
    a['title']='새기업, 캐나다 핵폐기물 처리용기 공급'
    a['body']='새기업은 91억원의 핵폐기물 저장용기 공급계약을 체결했다. 캐나다 원전에 공급하며 저장과 수송에 사용된다. 최종 수요자인 브루스파워는 추가 공급을 기대했다.'
    fact=f.extract(a)['primary']
    assert fact and 'counterparty' not in fact['fields'] and 'contract_end' not in fact['fields']


def test_rounding_requires_corrobating_contract_identity():
    rows=[article('a',money='91억원'),article('b',money='91.1억원'),article('c',money='91억773만원')]
    groups=f.cluster(rows)
    assert len(groups)==1 and groups[0]['article_count']==3
    assert groups[0]['primary_fact']['fields']['amount_krw']=='9107730000'
    assert groups[0]['primary_fact_source_url']=='c'
    assert {m['fact']['fields']['amount_krw'] for m in groups[0]['members']}=={'9100000000','9110000000','9107730000'}


def test_changed_party_amount_and_period_are_separate_contracts():
    for other in [article('b',party='다른가온테크'),article('b',money='110억원'),article('b',end='2020년 12월31일')]:
        assert len(f.cluster([article(),other]))==2


def test_different_products_are_not_merged():
    assert len(f.cluster([article(),article('b',product='이차전지 제조장비')]))==2


def test_cancelled_contract_is_separate_from_announcement():
    cancelled=article('b');cancelled['title']+=' 해지'
    assert len(f.cluster([article(),cancelled]))==2
    assert f.extract(cancelled)['primary']['stage']=='cancelled'


def test_unconfirmed_supply_and_contract_update_are_held():
    a=article();a['body']='제품 공급 시장이 커질 전망이다.'
    assert f.extract(a)['reason']=='contract_not_confirmed_in_body'
    a=article();a['title']+=' 계약기간 연장'
    assert f.extract(a)['reason']=='unsupported_contract_update'


def test_foreign_currency_is_not_interpreted_as_won():
    a=article(money='1억 달러')
    assert f.extract(a)['reason']=='unsupported_contract_currency'


def test_invalid_contract_date_is_held():
    assert f.extract(article(end='2019년 2월30일'))['reason']=='invalid_contract_period'


def test_money_resolution_and_number_canonicalization():
    assert f.number('91.1억')=='9110000000'
    assert f.money_resolution('91억원')=='100000000'
    assert f.money_resolution('91.1억원')=='10000000'
    assert f.money_resolution('91억773만원')=='10000'


@pytest.mark.parametrize('raw,value,resolution',[
    ('3천731억원','373100000000','100000000'),
    ('3731억887만4433원','373108874433','1'),
    ('2조3천450억6천700만8900원','2345067008900','1'),
    ('7천만원','70000000','10000000'),
    ('1억2천원','100002000','1000'),
    ('3,731억 887만 4,433원','373108874433','1'),
    ('1.25억원','125000000','1000000'),
])
def test_compound_contract_amount_is_consumed_in_full(raw,value,resolution):
    a=article(money=raw)
    fact=f.extract(a)['primary']
    assert fact['fields']['amount_krw']==value
    assert fact['evidence']['amount_krw']==raw
    assert f.money_resolution(raw)==resolution


def test_full_compound_contract_amount_wins_over_headline_and_revenue():
    a=article(money='3731억원')
    a['body']='최근 매출액은 5000억원이다. '+a['body'].replace('계약금액은 3731억원','계약금액은 3731억887만4433원')
    fact=f.extract(a)['primary']
    assert fact['fields']['amount_krw']=='373108874433'
    assert fact['amount_resolution_krw']=='1'


def test_compound_precision_does_not_merge_conflicting_won_amounts():
    a=article('a',money='3731억887만4433원')
    b=article('b',money='3731억887만4434원')
    assert len(f.cluster([a,b]))==2


def test_precise_won_amounts_are_not_merged_as_roughly_equal():
    assert len(f.cluster([article('a',money='100000000원'),article('b',money='100100000원')]))==2


def test_contract_period_does_not_consume_next_sentence_date():
    a=article();a['body']='새기업은 가온테크와 반도체 장비 공급계약을 체결했다. 계약금액은 100억원이다. 계약기간은 2019년 12월31일까지다. 다른 계획은 2020년 2월1일 발표한다.'
    x=f.extract(a)['primary']
    assert x['fields']['contract_end']=='2019-12-31' and 'contract_start' not in x['fields']


def multi_article():
    a=article()
    a['title']='새기업, 광섬유 공급계약 2건 체결'
    a['body']=('새기업은 첫가온테크와 광섬유를 유럽 지역에 공급하는 계약을 체결했다고 공시했다. '
               '계약금액은 100억원이다. 계약기간은 2019년 12월30일까지다. '
               '또, ACME Industrial Inc.와 광섬유를 이란 지역에 공급하는 계약을 체결했다. '
               '계약금액은 200억원이다. 계약기간은 2020년 12월31일까지다.')
    return a


def test_multiple_contracts_keep_party_amount_region_and_period_separate():
    a=multi_article()
    parsed=f.extract(a)
    assert parsed['primary'] is None and parsed['status']=='extracted_multiple_candidates'
    facts=f.fact_list(parsed)
    assert len(facts)==2 and all(x['target']=='광섬유' for x in facts)
    assert [(x['fields']['counterparty'],x['fields']['amount_krw'],x['fields']['delivery_region'],x['fields']['contract_end']) for x in facts]==[
        ('첫가온테크','10000000000','유럽','2019-12-30'),('ACME Industrial Inc.','20000000000','이란','2020-12-31')]
    assert '200억원' not in facts[0]['source_excerpt']
    assert '100억원' not in facts[1]['source_excerpt']


def test_aggregate_or_incompletely_split_contracts_are_held():
    a=multi_article()
    a['body']='새기업은 첫가온테크, 둘째가온테크와 각각 100억원, 200억원 규모 광섬유 공급계약 2건을 체결했다.'
    result=f.extract(a)
    assert not f.fact_list(result) and result['reason']=='multiple_contracts_not_safely_separable'
    a=multi_article();a['body']=a['body'].replace('계약금액은 200억원이다.','계약금액은 미공개이다.')
    assert not f.fact_list(f.extract(a))
    a=multi_article();a['title']=a['title'].replace('2건','3건')
    assert not f.fact_list(f.extract(a))


def test_multi_contract_groups_and_novelty_use_member_fact_index():
    a=multi_article()
    groups=f.cluster([a])
    assert len(groups)==2 and len({g['group_id'] for g in groups})==2
    assert all(g['article_count']==g['publisher_count']==1 for g in groups)
    assert {g['members'][0]['fact_index'] for g in groups}=={0,1}
    old={**a,'url':'old','published_at':'2018-05-20T10:00:00+09:00'}
    for group in groups:
        result=f.novelty(group,{'a':a},[old])
        assert result['value']==0 and result['supported_baseline_articles']==1
        assert result['supported_baseline_facts']==2


def test_sensitivity_compares_fact_members_instead_of_shared_article_url():
    import validate_np as v
    groups=[{'group_id':str(i),'title':'계약','member_urls':['a'],
             'member_keys':[str(('a',i))],'N':0,'P':None} for i in range(2)]
    check=v.compare_memberships({'groups':groups},[{'result':{'groups':groups},
                               'group_similarity_cut':0.42,'p_observation_max_hours':72}])
    assert all(g['stable_membership'] for g in check)


def test_multifact_evaluation_checks_cardinality_and_role_assignment():
    import evaluate_holdout as h
    a=multi_article()
    labels={'a':{'url':'a','has_primary_fact':True,'group':'two','requires_split':True,
                 'expected':{},'expected_facts':[
                     {'expected':{'fields.counterparty':'ACME Industrial Inc.','fields.amount_krw':'20000000000'}},
                     {'expected':{'fields.counterparty':'첫가온테크','fields.amount_krw':'10000000000'}}]}}
    assert h.evaluate([a],labels)['correct_extractions']==1
    labels['a']['expected_facts'][0]['expected']['fields.amount_krw']='10000000000'
    assert h.evaluate([a],labels)['correct_extractions']==0
    labels['a']['expected_facts'].pop()
    assert 'fact_cardinality_mismatch' in h.evaluate([a],labels)['articles'][0]['errors']


def test_body_product_replaces_headline_company_and_compound_amount():
    a=article(money='3천731억원')
    a['title']='새기업, 미국 업체와 3천731억원 부품 공급계약'
    a['body']='새기업은 가온테크와 3천731억원 규모의 부품 공급계약을 체결했다. 계약기간은 2019년 12월31일까지다.'
    assert f.extract(a)['primary']['target']=='부품'


@pytest.mark.parametrize('name',['광섬유 공급계약체결의 건','광섬유 공급 계약 체결의 건'])
def test_filing_product_drops_administrative_suffix(name):
    a=article();a['body']+=' 계약명 : '+name
    assert f.extract(a)['primary']['target']=='광섬유'


def comparison_article(url='a',name='한글발주',product='기계부품'):
    a=article(url,party=name,product=product)
    a['body']=a['body'].replace(name+'와',name+'(ACME Equipment Ltd.)와')
    a['body']+=' 기존 펌프(Pump), 회전자(Rotor)를 생산 공급할 예정이다.'
    return a


def test_bilingual_aliases_link_explicit_english_party_and_preserve_raw_fields():
    a=comparison_article()
    b=comparison_article('b',name='다른한글표기',product='펌프회전자부품')
    c=article('c',party='ACME Equipment Ltd.',product='기계부품')
    c['body']+=' 기존 펌프(Pump), 회전자(Rotor)를 생산 공급할 예정이다.'
    groups=f.cluster([c,b,a])
    assert len(groups)==1 and groups[0]['article_count']==3
    assert {m['fact']['fields']['counterparty'] for m in groups[0]['members']}=={'한글발주','다른한글표기','ACME Equipment Ltd.'}
    assert all(m['fact']['evidence']['counterparty'] for m in groups[0]['members'])


@pytest.mark.parametrize('key,value',[
    ('amount_krw','12000000000'),('contract_end','2020-12-31'),
    ('contract_start','2017-01-01'),('currency','USD'),('delivery_region','유럽'),
])
def test_aliases_never_override_conflicting_contract_fields(key,value):
    a=f.extract(comparison_article())['primary']
    a['fields']['delivery_region']='미주'
    b=deepcopy(a);b['target']='펌프회전자부품';b['fields'][key]=value
    assert not f.compatible(a,b)


def test_shared_party_amount_and_date_do_not_merge_distinct_detailed_products():
    a=f.extract(article(product='자동차부품'))['primary']
    b=deepcopy(a);b['target']='반도체부품'
    assert not f.compatible(a,b)
    a['comparison_evidence']['product_terms']=['pump']
    b['comparison_evidence']['product_terms']=['pump','rotor']
    assert not f.compatible(a,b)


def test_more_general_product_needs_extra_contract_anchor():
    a=f.extract(article(product='부품'))['primary']
    b=deepcopy(a);b['target']='반도체부품'
    for x in [a,b]:
        x['fields'].pop('contract_start');x['fields'].pop('contract_end')
    assert not f.compatible(a,b)


def test_region_comparison_distinguishes_partial_lists_from_explicit_broad_scope():
    assert f.same_region('미주와 해외','미주, 아시아, 유럽외')
    assert f.same_region('미주, 아시아, 유럽','미주,아시아,유럽외')
    assert not f.same_region('미주 및 해외','유럽')
    assert not f.same_region('미주, 유럽','아시아, 유럽')


def test_unobserved_spelling_similarity_is_not_a_party_alias():
    a=f.extract(article(party='발주회사'))['primary']
    b=f.extract(article('b',party='발주회샤'))['primary']
    assert not f.compatible(a,b)
    assert f.party_key('ACME & OTHER Ltd.')!='acme'


def test_bilingual_alias_context_is_scoped_to_supplier():
    a=f.extract(comparison_article())['primary']
    context=f.alias_context([a])
    b=deepcopy(a);b['actor']='다른공급사';b['fields']['counterparty']='ACME Equipment Ltd.'
    b['comparison_evidence']['counterparty_aliases']=[]
    c=deepcopy(b);c['fields']['counterparty']='한글발주'
    assert not f.same_party(b,c,context)


def test_novelty_recognizes_prior_contract_despite_product_and_party_variants():
    current=comparison_article(product='기계부품')
    prior=comparison_article('old',name='다른한글표기',product='펌프회전자부품')
    prior['published_at']='2018-05-24T10:00:00+09:00'
    group=f.cluster([current])[0]
    result=f.novelty(group,{'a':current},[prior])
    assert result['value']==0 and result['core_seen'] is True


def test_cached_contract_grouping_regression_keeps_two_daehan_contracts_separate():
    import evaluate_holdout as h
    base=Path(__file__).resolve().parents[2]/'models/sudden/cache/naver'
    plan=base/'holdout/plan_v2.json'
    if not plan.exists() or not (base/'crawl.sqlite3').exists():
        pytest.skip('새 계약 검토 캐시 없음')
    cases=json.loads(plan.read_text(encoding='utf-8'))['cases']
    rows,coverage=h.load_case(base,cases[0])
    if not coverage['collection_complete']:
        pytest.skip('검토 표본 수집 미완료')
    groups=[g for g in f.cluster(rows) if g.get('primary_fact')]
    assert len(groups)==1 and groups[0]['article_count']==9
    rows,coverage=h.load_case(base,cases[1])
    if not coverage['collection_complete']:
        pytest.skip('검토 표본 수집 미완료')
    groups=[g for g in f.cluster(rows) if g.get('primary_fact')]
    assert len(groups)==2 and all(g['article_count']==3 for g in groups)
    assert {g['primary_fact']['fields']['delivery_region'] for g in groups}=={'유럽','이란'}


def test_cached_contract_regression_preserves_fields_and_grouping():
    import pytest
    import evaluate_holdout as h
    root=Path(__file__).resolve().parents[2]
    db_path=root/'models/sudden/cache/naver/crawl.sqlite3'
    if not db_path.exists():
        pytest.skip('기사 캐시 없음')
    fixture=Path(__file__).parent/'fixtures/supply_contract_review_cases.json'
    entries=json.loads(fixture.read_text(encoding='utf-8'))['articles']
    with sqlite3.connect(db_path) as db:
        source={url:json.loads(payload) for url,payload in db.execute('SELECT url,payload FROM articles')}
    if any(e['url'] not in source for e in entries):
        pytest.skip('계약 회귀 사례 일부 없음')
    assert all(h.article_hash(source[e['url']])==e['article_sha256'] for e in entries)
    result=h.evaluate([source[e['url']] for e in entries],{e['url']:e for e in entries})
    assert result['correct_extractions']==12 and result['field_error_articles']==0
    assert result['grouping']['true_positive_pairs']==55
    assert result['grouping']['false_merge_pairs']==result['grouping']['false_split_pairs']==0
