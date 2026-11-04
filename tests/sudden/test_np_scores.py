"""시점 누수·관찰시간 차이·동일 언론사 반복·유보를 확인한다."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import np_scores as s
import news_review as r

EVENT = {'stock_code': '053660', 'n_base_start': '2018-06-17T15:30:00+09:00',
         'p_base_start': '2018-09-15T15:30:00+09:00', 'base_end': '2018-12-14T15:30:00+09:00',
         'news_window_end': '2018-12-19T15:30:00+09:00'}
CONFIG = {'p_observation_max_hours': 72, 'small_baseline_warning_below': 20}
BODY = '현진소재는 관계사 무한광업 서울사무소를 개소하고 금왕광산의 금 채굴을 준비한다고 밝혔다.'


def article(url, time, publisher='A', body=BODY):
    return {'url': url, 'published_at': time, 'modified_at': None, 'title': '현진소재 사무소 개소',
            'publisher': publisher, 'body': body, 'status': 'complete'}


def calculate(articles, event=EVENT, ready=True):
    rows, _ = r.prepare(articles, event, ['현진소재'])
    report = {'drop_id': '053660_20181219', 'event': event, 'coverage': {}, 'ready_for_baseline_analysis': ready}
    return s.score(report, rows, CONFIG)


def basic():
    return [article('old', '2018-10-01T14:00:00+09:00'), article('event', '2018-12-17T14:00:00+09:00')]


def test_identical_past_text_is_zero_novelty_not_new_topic():
    result = calculate(basic())
    group = result['groups'][0]
    assert group['N']['value'] == 0 and group['N']['nearest_prior_url'] == 'old'
    assert result['scale_status'] == 'provisional'
    assert result['integrated']['status'] == 'not_computed'


def test_no_baseline_is_held_not_maximum_novelty_or_zero_spread():
    group = calculate(basic()[1:])['groups'][0]
    assert group['N']['status'] == group['P']['status'] == 'held'
    assert group['N']['value'] is None and group['P']['value'] is None


def test_incomplete_collection_and_no_event_news_are_held():
    assert calculate(basic(), ready=False)['hold_reason'] == 'collection_incomplete'
    assert calculate(basic(), ready=False)['groups'] == []
    assert calculate(basic()[:1])['hold_reason'] == 'no_company_news_in_window'


def test_repeated_publisher_does_not_increase_p_and_ties_use_midrank():
    result = calculate(basic()+[article('repeat', '2018-12-17T15:00:00+09:00')])
    p = result['groups'][0]['P']
    assert p['publisher_count'] == 1 and p['value'] == 0.5 and p['tie_count'] == 1
    assert result['warnings'][0]['reason'] == 'small_baseline'


def test_reference_reports_are_observed_for_same_time_not_full_72_hours():
    event = {**EVENT, 'news_window_end': '2018-12-17T16:00:00+09:00'}
    articles = [article('old', '2018-10-01T14:00:00+09:00'),
                article('old_later', '2018-10-01T17:00:00+09:00', 'B'),
                article('event', '2018-12-17T14:00:00+09:00'),
                article('event_second', '2018-12-17T15:00:00+09:00', 'B')]
    p = calculate(articles, event)['groups'][0]['P']
    assert p['observation_seconds'] == 7200 and p['publisher_count'] == 2
    assert p['references'][0]['publisher_count'] == 1 and p['value'] == 1


def test_right_censored_reference_is_not_used_as_one_publisher_reference():
    articles = [article('old', '2018-12-14T14:00:00+09:00'), basic()[1]]
    p = calculate(articles)['groups'][0]['P']
    assert p['status'] == 'held' and p['censored_baseline_groups'] == 1


def test_group_started_before_p_base_is_not_counted_by_its_later_tail():
    articles = [article('before', '2018-09-15T14:00:00+09:00'),
                article('tail', '2018-09-15T16:00:00+09:00', 'B'), basic()[1]]
    p = calculate(articles)['groups'][0]['P']
    assert p['baseline_group_count'] == 0 and p['value'] is None


def test_future_articles_and_late_modifications_cannot_change_scores():
    base = basic()
    before = calculate(base)
    future = article('future', '2018-12-19T16:00:00+09:00', 'future')
    modified = {**article('modified', '2018-12-17T14:30:00+09:00', 'future'),
                'modified_at': '2018-12-19T16:00:00+09:00'}
    assert calculate(base+[future, modified]) == before


def test_stale_period_labels_do_not_admit_future_article():
    rows, _ = r.prepare(basic(), EVENT, ['현진소재'])
    future = {**rows[-1], 'url': 'future', 'published_at': '2018-12-20T14:00:00+09:00'}
    assert not s.eligible_rows([future], {'event_window'}, EVENT)


def test_missing_publisher_holds_p_but_keeps_n():
    articles = basic()
    articles[-1]['publisher'] = None
    result = calculate(articles)
    assert result['status'] == 'partial'
    assert result['groups'][0]['N']['status'] == 'computed'
    assert result['groups'][0]['P']['hold_reason'] == 'event_publisher_unknown'


@pytest.mark.parametrize('hours', [0, -1, 73, True])
def test_observation_config_cannot_exceed_available_grouping_horizon(hours):
    with pytest.raises(ValueError):
        s.score({'event': EVENT}, [], {**CONFIG, 'p_observation_max_hours': hours})
