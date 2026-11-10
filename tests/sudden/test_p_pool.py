"""합산 P 기준: 시점 규칙, 종목 평균 보정, 표본 부족 유보를 확인한다."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import p_pool as pp

EVENT = {'stock_code': 'AAA', 'base_end': '2018-12-14T15:30:00+09:00',
         'news_window_end': '2018-12-19T15:30:00+09:00'}


def group(code, start, publishers, fact=True, modified=None, gid=None):
    return {'stock_code': code, 'group_id': gid or f'{code}-{start}-{len(publishers)}', 'is_fact': fact,
            'first_published_at': start,
            'members': [{'url': f'{code}{start}{p}', 'publisher': p, 'published_at': start, 'modified_at': modified}
                        for p in publishers]}


def event_group(publishers):
    return {'first_published_at': '2018-12-17T14:00:00+09:00',
            'members': [{'url': p, 'publisher': p, 'published_at': '2018-12-17T14:00:00+09:00'} for p in publishers]}


def day(i):
    return f'2018-{1 + i // 28:02d}-{1 + i % 28:02d}T10:00:00+09:00'


def test_unscaled_percentile_uses_other_stocks_groups():
    pool = [group('BBB', day(i), ['x']) for i in range(10)] + [group('BBB', day(20 + i), ['x', 'y', 'z']) for i in range(10)]
    p = pp.pooled_propagation(event_group(['a', 'b']), EVENT, pool, 72)
    assert p['status'] == 'computed' and p['baseline_group_count'] == 20 and p['value'] == 0.5


def test_scale_compares_against_each_stocks_own_level():
    # 큰 종목 BBB는 늘 3곳, 작은 종목 AAA는 늘 1곳이 보도한다. AAA에서 2곳이면 평소의 2배다.
    pool = [group('BBB', day(i), ['x', 'y', 'z']) for i in range(20)] + [group('AAA', day(40 + i), ['a']) for i in range(5)]
    none = pp.pooled_propagation(event_group(['a', 'b']), EVENT, pool, 72, min_groups=1)
    scaled = pp.pooled_propagation(event_group(['a', 'b']), EVENT, pool, 72, scale='stock_mean', min_groups=1)
    assert none['value'] < 0.25 and scaled['value'] == 1.0
    assert scaled['own_stock_level'] == 1.0 and scaled['event_score'] == 2.0


def test_future_censored_old_and_late_modified_groups_are_not_used():
    ok = [group('BBB', day(i), ['x']) for i in range(20)]
    future = group('BBB', '2018-12-15T10:00:00+09:00', ['x', 'y'])
    censored = group('BBB', '2018-12-13T10:00:00+09:00', ['x', 'y'])
    old = group('BBB', '2017-06-01T10:00:00+09:00', ['x', 'y'])
    late = group('BBB', day(25), ['x', 'y'], modified='2018-12-18T10:00:00+09:00')
    base = pp.pooled_propagation(event_group(['a']), EVENT, ok, 72)
    more = pp.pooled_propagation(event_group(['a']), EVENT, ok + [future, censored, old, late], 72)
    assert more['value'] == base['value'] and more['baseline_group_count'] == 20
    assert more['skipped_baseline_groups'] == {'censored': 2, 'modified_after_cutoff': 1}


def test_non_fact_groups_feed_level_but_not_reference():
    pool = [group('BBB', day(i), ['x']) for i in range(20)] + [group('AAA', day(30), ['a', 'b', 'c', 'd'], fact=False)]
    p = pp.pooled_propagation(event_group(['a', 'b']), EVENT, pool, 72, scale='stock_mean')
    assert p['baseline_group_count'] == 20 and p['own_stock_level'] == 4.0 and p['event_score'] == 0.5


def test_small_pool_and_missing_own_level_are_held():
    pool = [group('BBB', day(i), ['x']) for i in range(5)]
    assert pp.pooled_propagation(event_group(['a']), EVENT, pool, 72)['hold_reason'] == 'pooled_reference_sample_too_small'
    assert pp.pooled_propagation(event_group(['a']), EVENT, pool, 72, scale='stock_mean')['hold_reason'] == 'no_own_stock_level'


@pytest.mark.parametrize('kwargs', [{'scale': 'median'}, {'days': 0}, {'days': True}])
def test_bad_settings_raise(kwargs):
    with pytest.raises(ValueError):
        pp.pooled_propagation(event_group(['a']), EVENT, [], 72, **kwargs)
