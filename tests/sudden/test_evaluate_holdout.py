"""유보를 성공으로 세지 않고, 동결 변경·라벨 변경을 탐지하는지 검증."""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'models/sudden'))
import evaluate_holdout as h


def article(url='a',amount=100):
    return {'url':url,'title':f'새회사, {amount}억 유상증자 결정','body':f'새회사는 {amount}억원 유상증자를 결정했다.',
            'published_at':'2018-05-25T10:00:00+09:00','modified_at':None,'publisher':'A'}


def label(a,group='same'):
    return {'url':a['url'],'article_sha256':h.article_hash(a),'has_primary_fact':True,'group':group,
            'expected':{'actor':'새회사','action':'equity_issue','target':'paid_in_equity'}}


def plan():
    return {'rule_sha256':{name:hashlib.sha256((h.HERE/name).read_bytes()).hexdigest() for name in
            ('news_facts.py','news_review.py','np_scores.py','np_config.json')},'cases':[{'drop_id':'test'}]}


def test_held_contracts_lower_coverage_and_are_not_perfect_grouping():
    a=article()
    b={**article('b'),'title':'새회사, 공급계약 체결'}
    c={**b,'url':'c'}
    labels={a['url']:label(a,'raise'),b['url']:{**label(b,'contract'),'expected':{'actor':'새회사','action':'supply_contract','target':'equipment'}},
            c['url']:{**label(c,'contract'),'expected':{'actor':'새회사','action':'supply_contract','target':'equipment'}}}
    result=h.evaluate([a,b,c],labels)
    assert result['correct_extractions']==1 and result['fact_coverage']==1/3
    assert result['held_fact_articles']==2
    assert result['grouping']['recall']==0 and result['grouping']['precision'] is None


def test_false_merges_and_splits_are_reported():
    a,b=article(),article('b')
    merged=h.evaluate([a,b],{'a':label(a,'first'),'b':label(b,'second')})
    assert merged['grouping']['false_merge_pairs']==1
    b=article('b',200)
    split=h.evaluate([a,b],{'a':label(a),'b':label(b)})
    assert split['grouping']['false_split_pairs']==1


def test_field_error_is_not_correct_extraction():
    a=article()
    wrong={**label(a),'expected':{'actor':'다른회사','action':'equity_issue','target':'paid_in_equity'}}
    result=h.evaluate([a],{'a':wrong})
    assert result['field_error_articles']==1 and result['correct_extractions']==0


def test_article_hash_ignores_collection_time_but_not_changed_body():
    a=article()
    assert h.article_hash(a)==h.article_hash({**a,'collected_at':'tomorrow'})
    assert h.article_hash(a)!=h.article_hash({**a,'body':'다른 본문'})


def test_changed_rule_blocks_evaluation(monkeypatch):
    p=plan();p['rule_sha256']['news_facts.py']='changed'
    monkeypatch.setattr(h,'load_case',lambda *args:(_ for _ in ()).throw(AssertionError('must not load data')))
    result=h.run(Path('.'),p,{'articles':[]})
    assert result['status']=='rules_changed'


def test_missing_and_stale_annotations_block_predictions(monkeypatch):
    a=article()
    monkeypatch.setattr(h,'load_case',lambda *args:([a],{'collection_complete':True}))
    result=h.run(Path('.'),plan(),{'articles':[]})
    assert result['cases'][0]['status']=='annotation_incomplete'
    stale={**label(a),'article_sha256':'old'}
    result=h.run(Path('.'),plan(),{'articles':[stale]})
    assert result['cases'][0]['stale_labels']==['a']


def test_empty_manifest_and_duplicate_labels_are_rejected():
    assert h.run(Path('.'),{'cases':[],'rule_sha256':{}},{'articles':[]})['status']=='invalid_freeze_manifest'
    a=article()
    assert h.run(Path('.'),plan(),{'articles':[label(a),label(a)]})['status']=='invalid_annotations'


def test_multi_contract_article_cannot_pass_as_one_correct_fact():
    a=article()
    annotation={**label(a),'requires_split':True}
    result=h.evaluate([a],{'a':annotation})
    assert result['multi_fact_articles']==1 and result['correct_extractions']==0
    assert 'multiple_primary_facts_not_representable' in result['articles'][0]['errors']
    assert result['grouping']['precision'] is None


def test_explicit_aliases_are_accepted_but_other_values_are_not():
    a=article()
    annotation={**label(a),'expected':{'actor':'새 회사','action':'equity_issue','target':'paid_in_equity'},
                'accepted_values':{'actor':['새회사']}}
    assert h.evaluate([a],{'a':annotation})['correct_extractions']==1
    annotation['accepted_values']['actor']=['무관한회사']
    assert h.evaluate([a],{'a':annotation})['correct_extractions']==0


def test_field_diagnostics_distinguish_partial_matches_and_abstentions():
    a=article()
    b={**article('b'),'title':'새회사, 미지원 소식'}
    wrong={**label(a),'expected':{'actor':'다른회사','action':'equity_issue','target':'paid_in_equity'}}
    result=h.evaluate([a,b],{'a':wrong,'b':label(b)})
    fields=result['field_checks_single_fact_articles']
    assert fields['actor']=={'checked':2,'matched':0,'abstained':1,'missing':0}
    assert fields['action']['matched']==1 and fields['action']['abstained']==1
