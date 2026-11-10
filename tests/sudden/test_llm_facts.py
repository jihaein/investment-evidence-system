"""LLM 사실 추출: 근거 문구 확인, 규칙 모양 변환, 캐시 재사용을 외부 접속 없이 확인한다."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import llm_facts as lf

ARTICLE = {'url': 'u1', 'title': 'DMS, 61억원 규모 장비 공급계약 체결', 'body': 'DMS는 중국 업체와 61억원 규모 공급계약을 맺었다고 밝혔다.',
           'published_at': '2018-03-28T09:00:00+09:00', 'modified_at': None, 'publisher': 'A'}


def fact(**kw):
    base = {'actor': 'DMS', 'action': 'supply_contract', 'other_kind': None, 'target': '장비', 'stage': 'announced',
            'fields': {k: None for k in lf.FIELDS} | {'amount_krw': '6100000000'}, 'evidence': '61억원 규모 장비 공급계약 체결'}
    return base | kw


def test_fact_with_quote_in_article_is_kept_in_rule_shape():
    out = lf.to_rule_shape({'has_fact': True, 'facts': [fact()]}, ARTICLE)
    assert out['status'] == 'extracted_candidate' and out['primary']['fields'] == {'amount_krw': '6100000000'}
    assert out['facts'] == [] and out['primary']['extracted_by'] == 'llm'


def test_fact_whose_quote_is_not_in_article_is_dropped():
    out = lf.to_rule_shape({'has_fact': True, 'facts': [fact(evidence='2019년 계약 해지')]}, ARTICLE)
    assert out['primary'] is None and out['reason'] == 'llm_evidence_not_in_article'


def test_other_action_is_counted_not_used():
    out = lf.to_rule_shape({'has_fact': True, 'facts': [fact(action='other', other_kind='무상증자')]}, ARTICLE)
    assert out['primary'] is None and out['other_kinds'] == ['무상증자'] and out['reason'] == 'llm_other_action'


def test_answers_are_cached_and_not_asked_twice(tmp_path):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        msg = SimpleNamespace(content=json.dumps({'has_fact': True, 'facts': [fact()]}), refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    ex = lf.Extractor('gpt-5-nano', tmp_path, client)
    assert ex(ARTICLE)['primary']['actor'] == 'DMS' and ex(ARTICLE)['primary']['actor'] == 'DMS'
    assert len(calls) == 1 and calls[0]['reasoning_effort'] == 'minimal'
    assert lf.Extractor('gpt-5-nano', tmp_path, None)(ARTICLE)['primary']['target'] == '장비'


def test_invented_contract_year_and_null_strings_are_dropped():
    f = fact(fields={k: None for k in lf.FIELDS} | {'contract_end': '2026-11-05', 'counterparty': 'null'})
    out = lf.to_rule_shape({'has_fact': True, 'facts': [f]}, ARTICLE)
    assert 'contract_end' not in out['primary']['fields'] and 'counterparty' not in out['primary']['fields']
    assert out['dropped_implausible_fields'] == ['contract_end']
    ok = fact(fields={k: None for k in lf.FIELDS} | {'contract_end': '2018-11-05'}, target='공정 장비', actor='DMS(068790)')
    out = lf.to_rule_shape({'has_fact': True, 'facts': [ok]}, ARTICLE)
    assert out['primary']['fields']['contract_end'] == '2018-11-05'
    assert out['primary']['target'] == '공정장비' and out['primary']['actor'] == 'DMS'


def test_hybrid_uses_llm_only_when_rule_does_not_know_the_action():
    asked = []

    def llm(article):
        asked.append(article['url'])
        return lf.to_rule_shape({'has_fact': True, 'facts': [fact()]}, article)

    def rule(article):
        reason = {'u1': 'unsupported_title_action', 'u2': 'contract_amount_not_identified'}.get(article['url'])
        if reason:
            return {'status': 'needs_review', 'primary': None, 'facts': [], 'reason': reason}
        return {'status': 'extracted_candidate', 'primary': {'action': 'mou'}, 'facts': [], 'reason': None}

    h = lf.Hybrid(llm, rule)
    assert h(ARTICLE)['primary']['extracted_by'] == 'llm' and h(ARTICLE)['rule_reason'] == 'unsupported_title_action'
    assert h(ARTICLE | {'url': 'u2', 'title': 'x'})['primary'] is None
    assert h(ARTICLE | {'url': 'u3', 'title': 'y'})['primary'] == {'action': 'mou'}
    assert asked == ['u1'] and h.used == {'llm_fact': 1, 'rule_hold': 1, 'rule_fact': 1}


def test_fact_without_target_is_dropped_like_rules():
    out = lf.to_rule_shape({'has_fact': True, 'facts': [fact(target=None)]}, ARTICLE)
    assert out['primary'] is None and out['reason'] == 'llm_target_not_identified' and out['dropped_no_target'] == 1
