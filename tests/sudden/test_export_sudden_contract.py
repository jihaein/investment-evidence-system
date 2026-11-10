import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import json

import jsonschema
import pytest

import export_sudden_contract as ec

SCHEMA = json.loads((Path(__file__).resolve().parents[2] / 'contracts/sudden_result.schema.json').read_text(encoding='utf-8'))
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA, format_checker=jsonschema.FormatChecker())


def _result(verdict='가치훼손', hypothesis='H1', reason=None, units=None):
    return {'drop_id': '000070_20230208', 'stock_code': '000070', 'company_name': 'A사', 'D': '2023-02-08', 'z': -4.0,
            'rule_version': 'r', 'prompt_version': 'p', 'model': 'm', 'inputs': {'news_docs': 1, 'dart': 1, 'market_rules': 1},
            'support': {'H1': 1.0}, 'refute': {'H3': 1.0}, 'hypothesis': hypothesis, 'verdict': verdict, 'reason': reason,
            'runner_up': None, 'flags': {'fragile': False, 'unverified_basis': False, 'mixed': [], 'low_confidence': False},
            'units': units if units is not None else [
                {'unit_id': 'u1', 'type': 'F1', 'direction': '부정', 'event': 'A사-실적', 'quote': '기사 문장', 'w': 0.6,
                 'certainty': '확정', 'mandatory': False, 'members': [{'bundle_id': 'b1', 'url': 'https://n.news/1'}]},
                {'unit_id': 'u2', 'type': 'F1', 'direction': '부정', 'event': 'A사-잠정실적', 'quote': '2022년 영업이익 -62%', 'w': 1.0,
                 'certainty': '확정', 'mandatory': True, 'members': [{'bundle_id': 'dart:1', 'url': 'https://dart/1'}]},
                {'unit_id': 'k1', 'type': 'K4', 'direction': '없음', 'event': 'K4', 'quote': 'AR 12%', 'w': 0.6,
                 'market_rule': True, 'members': []}]}


def _contract(cases):
    return {'schema_version': '0.1', 'generated_at': 'now', 'rule_version': 'r', 'sample': {'name': 't', 'split': None},
            'params': {}, 'cases': cases}


def test_case_valid_and_no_article_text():
    c = ec.case_out(_result(), {'market': 'KOSPI', 'decision_time': '2023-02-08T15:30:00+09:00', 'ar_pct': '-5.1'})
    VALIDATOR.validate(_contract([c]))
    src = {u['unit_id']: u for u in c['evidence']}
    assert [src[k]['source'] for k in ('u1', 'u2', 'k1')] == ['news', 'dart', 'market']
    assert src['u1']['filing'] is None                    # 기사 발췌는 넣지 않는다
    assert src['u2']['filing'] == '2022년 영업이익 -62%'
    assert c['scores']['support']['H2'] == 0.0


def test_hold_with_h0_maps_to_null():
    c = ec.case_out({**_result('판단보류', 'H0', 'no_evidence_in_window', []), 'support': None, 'refute': None,
                     'flags': None, 'inputs': None}, {})
    VALIDATOR.validate(_contract([c]))
    assert c['hypothesis'] is None and c['hold_reason'] == 'no_evidence_in_window'
    assert c['decision_time'] == '2023-02-08T15:30:00+09:00'


def test_schema_rejects_inconsistent_verdict():
    c = ec.case_out(_result(), {})
    bad = {**c, 'verdict': '비가치'}                       # 비가치인데 H1
    with pytest.raises(jsonschema.ValidationError):
        VALIDATOR.validate(_contract([bad]))
    with pytest.raises(jsonschema.ValidationError):
        VALIDATOR.validate(_contract([{**c, 'verdict': '판단보류', 'hold_reason': None}]))


def test_sell_schedule_first_matching_drop_per_stock():
    cases = [{'stock_code': 'A', 'drop_date': '2017-05-01', 'verdict': '가치훼손', 'flags': {'fragile': True}},
             {'stock_code': 'A', 'drop_date': '2016-03-01', 'verdict': '비가치', 'flags': {'fragile': False}},
             {'stock_code': 'A', 'drop_date': '2018-01-02', 'verdict': '가치훼손', 'flags': {'fragile': False}},
             {'stock_code': 'B', 'drop_date': '2016-02-01', 'verdict': '판단보류', 'flags': {'fragile': False}}]
    assert ec.sell_schedule({'cases': cases}) == {'A': '2017-05-01'}
    assert ec.sell_schedule({'cases': cases}, skip_fragile=True) == {'A': '2018-01-02'}
