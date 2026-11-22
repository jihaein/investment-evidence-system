import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import numpy as np
import pandas as pd
import pytest

import long_history as lh

DAYS = pd.bdate_range('2024-01-01', periods=30)


def grids():
    r = pd.DataFrame(0.0, index=DAYS, columns=['000010', '000020'])
    mk = pd.DataFrame(0.0, index=DAYS, columns=['KOSDAQ'])
    return r, mk


def test_forward_window_starts_after_sell_day_and_subtracts_market():
    r, mk = grids()
    r.loc[DAYS[2], '000010'] = -0.10           # 매도일 당일: 창에 넣지 않는다
    r.loc[DAYS[3], '000010'] = -0.10           # 매도 다음날: 창에 들어간다
    mk.loc[DAYS[4], 'KOSDAQ'] = 0.05
    fw = lh.forward(r, mk, np.array(['000010']), np.array(['KOSDAQ']), [DAYS[2]], horizons=(5,))
    assert fw[5][0] == pytest.approx(-0.10 - 0.05)


def test_forward_skips_windows_past_data_end():
    r, mk = grids()
    fw = lh.forward(r, mk, np.array(['000010']), np.array(['KOSDAQ']), [DAYS[-3]], horizons=(5,))
    assert np.isnan(fw[5][0])


def test_forward_keeps_delisted_stock_flat_after_last_price():
    """상장폐지 뒤 시세가 없으면(NaN) 마지막 가격에서 현금: 사건을 빼지 않는다(생존 편향 방지)."""
    r, mk = grids()
    r.loc[DAYS[3], '000020'] = -0.50
    r.loc[DAYS[4]:, '000020'] = np.nan
    fw = lh.forward(r, mk, np.array(['000020']), np.array(['KOSDAQ']), [DAYS[2]], horizons=(10,))
    assert fw[10][0] == pytest.approx(-0.50)


def test_common_mask_drops_preferred_spac_reit_and_administrative():
    p = pd.DataFrame({'stock_code': ['000010', '000015', '000020', '000030', '000040'],
                      'company_name': ['보통', '보통우', '하나스팩1호', '보통', '보통'],
                      'sect': ['', '', '', '관리종목(소속부없음)', '']})
    assert lh.common_mask(p).tolist() == [True, False, False, False, True]


def test_regime_of_labels_named_periods():
    d = pd.Series(pd.to_datetime(['2020-03-15', '2023-05-01', '2022-06-30']))
    assert lh.regime_of(d).tolist() == ['2020 코로나', '그 외', '2021-22 긴축하락장']
