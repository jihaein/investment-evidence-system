"""민감도 결과가 분할·병합을 숨기지 않고 미완료 사건을 제외하는지 확인한다."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import validate_np as v
import news_review as r
import np_scores as np

EVENT = {'stock_code': '053660', 'n_base_start': '2018-06-17T15:30:00+09:00',
         'p_base_start': '2018-09-15T15:30:00+09:00', 'base_end': '2018-12-14T15:30:00+09:00',
         'news_window_end': '2018-12-19T15:30:00+09:00'}
CONFIG = {'p_observation_max_hours': 72, 'small_baseline_warning_below': 20, 'group_similarity_cut': 0.42}


def fixtures():
    body = '현진소재는 관계사 무한광업 서울사무소를 개소하고 금왕광산 금 채굴을 준비한다고 밝혔다.'
    articles = [{'url': url, 'published_at': date, 'modified_at': None, 'title': '현진소재 사무소 개소',
                 'body': text, 'publisher': publisher, 'status': 'complete'} for url,date,text,publisher in [
        ('old', '2018-10-01T14:00:00+09:00', body, 'A'),
        ('first', '2018-12-17T14:00:00+09:00', body, 'A'),
        ('second', '2018-12-17T15:00:00+09:00', body+' 관계자는 준비를 지속한다고 덧붙였다.', 'B')]]
    rows, _ = r.prepare(articles, EVENT, ['현진소재'])
    return {'drop_id': 'example', 'event': EVENT, 'coverage': {}, 'ready_for_baseline_analysis': True}, rows


def test_sweep_detects_split_without_changing_default_config():
    report, rows = fixtures()
    case, default = v.validate_case(report, rows, CONFIG, [0.1, 1.0], [24,72])
    assert len(case['scenarios']) == 4
    assert case['sensitivity_status'] == 'membership_changed'
    assert not case['membership_checks'][0]['stable_membership']
    assert default['config'] == CONFIG and CONFIG['group_similarity_cut'] == 0.42
    assert len(default['groups']) == 1


def test_incomplete_case_has_no_sweep_or_numeric_result():
    report, rows = fixtures()
    report['ready_for_baseline_analysis'] = False
    case, default = v.validate_case(report, rows, CONFIG, [0.35,0.5], [24,72])
    assert case['sensitivity_status'] == 'not_evaluated' and case['scenarios'] == []
    assert default['status'] == 'held' and default['groups'] == []


def test_same_membership_does_not_require_identical_metric_values():
    default = {'groups': [{'group_id': 'x', 'title': 'x', 'member_urls': ['a','b']}]}
    scenarios = [{'group_similarity_cut': 0.42, 'p_observation_max_hours': 24,
                  'result': {'groups': [{'group_id': 'x', 'member_urls': ['a','b'], 'N': 0.5, 'P': 0.3}]}},
                 {'group_similarity_cut': 0.42, 'p_observation_max_hours': 72,
                  'result': {'groups': [{'group_id': 'x', 'member_urls': ['a','b'], 'N': 0.5, 'P': 0.8}]}}]
    check = v.compare_memberships(default, scenarios)[0]
    assert check['stable_membership'] and check['P_range_across_matched_groups'] == [0.3,0.8]


@pytest.mark.parametrize('cut', [0, -0.1, 1.1, True])
def test_similarity_cut_is_validated_before_scoring(cut):
    with pytest.raises(ValueError):
        np.score({}, [], {**CONFIG, 'group_similarity_cut': cut})


def test_invalid_grid_is_rejected_even_for_uncollected_case():
    report, rows = fixtures()
    report['ready_for_baseline_analysis'] = False
    with pytest.raises(ValueError):
        v.validate_case(report, rows, CONFIG, [-0.1], [24])
    with pytest.raises(ValueError):
        v.validate_case(report, rows, CONFIG, [0.42], [73])
