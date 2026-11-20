import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import numpy as np
import pandas as pd
import pytest

import joint_backtest as jb
import stress as st

DAYS = pd.bdate_range('2020-01-02', periods=600)


def _grid(n=6, seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame(rng.normal(0, 2, (len(DAYS), n)), index=DAYS, columns=[f'00000{i}' for i in range(n)])


def test_metrics_match_team_definitions():
    W = pd.Series([100, 110, 99, 120], index=DAYS[:4], dtype=float)
    assert st.cumret(W) == pytest.approx(0.2) and st.mdd(W) == pytest.approx(1 - 99 / 110)
    r = W.pct_change().dropna()
    assert st.sharpe(W) == pytest.approx(r.mean() / r.std() * np.sqrt(252))


def test_schedules_snap_shift_and_stoploss():
    days = DAYS[:10]
    assert st.snap({'A': '2020-01-04', 'B': '2030-01-01'}, days, days[-1]) == {'A': pd.Timestamp('2020-01-06')}   # 토 → 월, 끝 넘으면 버림
    assert st.shift_schedule({'A': days[2]}, days, 1) == {'A': days[3]}
    basket = pd.DataFrame({'A': [0, -10, -15, 5], 'B': [1, 1, 1, 1]}, index=days[:4], dtype=float)
    assert st.stoploss_schedule(basket, days[0], days[3]) == {'A': days[2]}          # 0.9 × 0.85 = 0.765 ≤ 0.8


def test_sudden_schedule_first_value_impair_in_window():
    cases = [{'stock_code': 'A', 'verdict': '비가치', 'drop_date': '2020-02-03'},
             {'stock_code': 'A', 'verdict': '가치훼손', 'drop_date': '2020-03-02'},
             {'stock_code': 'A', 'verdict': '가치훼손', 'drop_date': '2020-04-01'},
             {'stock_code': 'B', 'verdict': '가치훼손', 'drop_date': '2019-12-02'}]
    out = st.sudden_schedule(cases, {'A', 'B'}, pd.Timestamp('2020-01-02'), pd.Timestamp('2020-12-31'))
    assert out == {'A': pd.Timestamp('2020-03-02')}


def test_run_stress_rows_and_summary_with_fake_alarm():
    eng = jb.load_engine()
    grid = _grid()
    alarm = lambda codes, entry, end: {codes[0]: entry + pd.Timedelta(days=30)}
    cases = [{'stock_code': c, 'verdict': '가치훼손', 'drop_date': '2020-06-01'} for c in grid.columns]
    d = st.run_stress(eng, grid, n_runs=4, n_pick=3, horizon_years=1, alarm_fn=alarm, cases=cases)
    assert set(d['strategy']) >= {'무손절', '단순손절', '임계', '급락', '임계_그리고_급락', '무작위매도_임계', '무작위매도_급락'}
    assert d['run'].nunique() == 4 and set(d['lag']) == {0, 1}
    assert (d[d['strategy'] == '임계']['매도수'] == 1).all()
    s = st.summary(d, lag=1)
    row = s[s['전략'] == '임계'].iloc[0]
    assert row['런수'] == 4 and 0 <= row['승률_vs무작위매도'] <= 1


def test_cash_effect_matches_team_definition():
    basket = pd.DataFrame({'A': [0.0, 10.0, -50.0, 20.0]}, index=DAYS[:4])
    v = st.cash_effect(basket, {'A': DAYS[1]}, DAYS[3], 0.0)
    assert v == pytest.approx(0 - (0.5 * 1.2 - 1))                 # 매도 다음 날부터 끝까지 보유했을 때보다 현금이 +40%p
    assert np.isnan(st.cash_effect(basket, {}, DAYS[3], 0.0))


def test_montecarlo_same_baskets_as_team_rng_and_summary_columns():
    eng = jb.load_engine()
    grid = _grid(n=8)
    alarm = lambda codes, entry, end: {codes[0]: entry + pd.Timedelta(days=40)}
    d, s = st.run_montecarlo(eng, grid, '2020-01-02', 1, n_pick=3, n_runs=5, alarm_fn=alarm)
    rng = np.random.default_rng(0)
    first = list(rng.choice(list(grid.columns), size=3, replace=False))
    assert d[d['run'] == 0]['매도수'].tolist()[2] == 1 and len(d) == 15
    assert list(s.index) == ['무손절', '단순손절', '임계']
    assert {'누적수익률', '수익률_중앙값', 'MDD', 'Sortino', 'Sharpe', '평균_현금대체', '평균_매도수', 'win_vs_무손절'} <= set(s.columns)
    assert s.loc['무손절', 'win_vs_무손절'] == 0 and first   # 같은 seed 0 순서로 뽑는다


def test_runs_are_paired_across_contracts():
    """판정 파일이 달라 매도 수가 달라도 같은 seed면 진입일·바스켓(무손절 결과)이 같아야 두 판을 짝지어 비교할 수 있다."""
    eng = jb.load_engine()
    grid = _grid(n=8)
    alarm = lambda codes, entry, end: {}
    one = [{'stock_code': c, 'verdict': '가치훼손', 'drop_date': '2020-06-01'} for c in grid.columns[:1]]
    many = [{'stock_code': c, 'verdict': '가치훼손', 'drop_date': '2020-06-01'} for c in grid.columns]
    a = st.run_stress(eng, grid, n_runs=5, n_pick=4, horizon_years=1, alarm_fn=alarm, cases=one)
    b = st.run_stress(eng, grid, n_runs=5, n_pick=4, horizon_years=1, alarm_fn=alarm, cases=many)
    pick = lambda d: d[(d['strategy'] == '무손절') & (d['lag'] == 1)][['entry', '누적수익률']].reset_index(drop=True)
    pd.testing.assert_frame_equal(pick(a), pick(b))


def test_slow_signals_and_combination_with_sudden():
    eng = jb.load_engine()
    grid = _grid(n=6)
    alarm = lambda codes, entry, end: {}
    cases = [{'stock_code': c, 'verdict': '가치훼손', 'drop_date': '2020-06-01'} for c in grid.columns]
    slow = lambda codes, entry, end: {'count': {codes[0]: pd.Timestamp('2020-09-01')}, 'sum': {codes[0]: pd.Timestamp('2020-04-01'),
                                                                                                codes[1]: pd.Timestamp('2020-12-01')}}
    d, s = st.run_montecarlo(eng, grid, '2020-01-02', 1, n_pick=3, n_runs=2, alarm_fn=alarm, cases=cases, slow_fn=slow)
    run0 = d[d['run'] == 0].set_index('strategy')['매도수']
    assert run0['서서히'] == 1 and run0['서서히_합산'] == 2 and run0['서서히_그리고_급락'] == 1 and run0['합산_그리고_급락'] == 2
    assert list(s.index)[-4:] == ['서서히', '서서히_합산', '서서히_그리고_급락', '합산_그리고_급락']


def test_seoseohi_first_signal_skips_hold_baseline():
    import seoseohi_signal as ss
    calls = {}

    def runner(code, buy, now):              # 2016-04-01 기준선은 유보, 6/1부터 판정. 2017-06-01에 세 모델 모두 악화
        calls[(code, buy, now)] = True
        if buy == '2016-04-01':
            return {'dupont': None, 'fscore': None, 'ohlson': None}
        bad = now >= '2017-06-01'
        return {'dupont': 3 if bad else 0, 'fscore': 3 if bad else 0, 'ohlson': 2 if bad else 0}
    sig = ss.Seoseohi(runner).first_signals(['A'], '2016-01-04', '2019-12-30')
    assert sig['count'] == {'A': pd.Timestamp('2017-06-01')} and sig['sum'] == {'A': pd.Timestamp('2017-06-01')}
