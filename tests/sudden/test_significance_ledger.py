import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import numpy as np
import pandas as pd
import pytest

import forward_ledger as fl
import significance as sg

DAYS = pd.bdate_range('2024-01-01', periods=30)


def test_forward_ar_starts_after_sell_day_and_subtracts_market():
    grid = pd.DataFrame(0.0, index=DAYS, columns=['000001', '000002'])
    grid.loc[DAYS[2], '000001'] = -10.0      # 매도일(급락 다음 거래일) 당일 손실은 피한 것이 아니다 → 넣지 않는다
    grid.loc[DAYS[3], '000001'] = -10.0      # 매도 다음날부터 창에 들어간다
    cases = [{'stock_code': '000001', 'drop_date': DAYS[1].strftime('%Y-%m-%d'), 'verdict': '가치훼손'}]
    ev = sg.forward_ar(grid, cases, horizon=5)
    market = (1 + grid.mean(axis=1) / 100).iloc[3:8].prod() - 1
    assert ev['ar'].iloc[0] == pytest.approx(-0.10 - market)


def test_forward_ar_skips_windows_past_data_end():
    grid = pd.DataFrame(0.0, index=DAYS, columns=['000001'])
    cases = [{'stock_code': '000001', 'drop_date': DAYS[-3].strftime('%Y-%m-%d'), 'verdict': '비가치'}]
    assert sg.forward_ar(grid, cases, horizon=5).empty


def test_cluster_bootstrap_resamples_whole_clusters():
    rng = np.random.default_rng(0)
    boot = sg.cluster_boot_mean(np.array([1.0, 1.0, 1.0, -1.0]), np.array(['a', 'a', 'a', 'b']), rng, n=2000)
    assert set(np.round(boot, 6)) <= {1.0, -1.0, 0.5}          # a·a, b·b, a·b 조합의 사건 가중 평균만 나온다


def test_describe_one_sided_direction():
    boot = np.array([-0.2, -0.1, 0.1])
    assert sg.describe(boot, -0.1, better='neg')['p_one_sided'] == pytest.approx(1 / 3)
    assert sg.describe(boot, -0.1, better='pos')['p_one_sided'] == pytest.approx(2 / 3)


def test_ledger_leaves_unmatured_horizons_blank():
    rets = pd.DataFrame(0.01, index=DAYS, columns=['000001'])
    index = pd.DataFrame(0.0, index=DAYS, columns=['KOSPI'])
    cases = pd.DataFrame([{'stock_code': '000001', 'market': 'KOSPI', 'drop_date': DAYS[20].strftime('%Y-%m-%d'),
                           'verdict': '가치훼손'}])
    led = fl.ledger(cases, rets, index, horizons=(5, 20))
    assert led['sell_date'].iloc[0] == DAYS[21].strftime('%Y-%m-%d')
    assert led['ar_5d'].iloc[0] == pytest.approx(1.01 ** 5 - 1)
    assert 'ar_20d' not in led
    s = fl.summarize(led, horizons=(5, 20))
    assert s[5]['가치훼손']['n'] == 1 and s[20] == {'matured': 0}


def test_latest_prices_picks_latest_end_date(tmp_path):
    for name in ('prices_20220601_20251231.csv', 'prices_20250519_20261120.csv', 'prices_20250601_20261106.csv'):
        (tmp_path / name).write_text('x')
    prices, index = fl.latest_prices(tmp_path)
    assert prices.name == 'prices_20250519_20261120.csv' and index.name == 'index_20250519_20261120.csv'
