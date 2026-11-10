import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import numpy as np
import pandas as pd
import pytest

import joint_backtest as jb


DATES = pd.DatetimeIndex(['2016-01-04', '2016-01-05', '2016-01-06', '2016-01-07'])


def test_sell_is_next_trading_day():
    assert jb.next_trading_day(DATES, '2016-01-04') == pd.Timestamp('2016-01-05')
    assert jb.next_trading_day(DATES, '2016-01-07') is None
    assert jb.to_schedule({'A': '2016-01-05', 'B': '2016-01-07'}, DATES) == {'A': pd.Timestamp('2016-01-06')}


def test_combine_or_and():
    a = {'A': pd.Timestamp('2016-03-01'), 'B': pd.Timestamp('2016-05-01')}
    b = {'A': pd.Timestamp('2016-02-01'), 'C': pd.Timestamp('2016-04-01')}
    assert jb.combine(a, b, 'or') == {'A': pd.Timestamp('2016-02-01'), 'B': pd.Timestamp('2016-05-01'),
                                      'C': pd.Timestamp('2016-04-01')}
    assert jb.combine(a, b, 'and') == {'A': pd.Timestamp('2016-03-01')}


def test_metrics_mdd_and_return():
    w = pd.Series([100, 110, 88, 99], index=DATES, dtype=float)
    m = jb.metrics(w, 0.0)
    assert m['cum_return'] == pytest.approx(-0.01)
    assert m['mdd'] == pytest.approx(88 / 110 - 1, abs=1e-4)


def test_avoided_return_after_sell_and_delisting():
    basket = pd.DataFrame({'A': [0, 10, -50, 20], 'B': [0, 5, np.nan, np.nan]}, index=DATES, dtype=float)
    rows = jb.avoided(basket, {'A': DATES[1], 'B': DATES[0]}, recovery_rate=0.0)
    by = {r['stock_code']: r for r in rows}
    assert by['A']['after_return'] == pytest.approx(0.5 * 1.2 - 1)    # 매도 다음 날부터
    assert by['B']['delisted_later'] and by['B']['after_return'] == -1.0   # 상장폐지, 회수율 0
