"""v3.1 급락 정의: 시장모형 AR(고베타 종목의 시장 급락일 오탐 제거)과 보통주·관리종목 거르기."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import events as ev


def prices(beta=2.0, n=200, crash_day=150):
    rng = np.random.default_rng(0)
    m = rng.normal(0, 0.01, n)
    m[crash_day] = -0.05                                    # 시장 급락일
    r = beta * m + rng.normal(0, 0.005, n)
    days = pd.bdate_range('2023-01-02', periods=n)
    return pd.DataFrame({'stock_code': '000010', 'company_name': 'A사', 'market': 'KOSDAQ', 'trade_date': days,
                         'close': 1000.0, 'ret': r, 'volume': 1.0, 'mktcap': 2000e8, 'sect': ''}), \
        pd.DataFrame({'trade_date': days, 'market': 'KOSDAQ', 'index_ret': m}), crash_day


def test_market_model_removes_high_beta_false_drop():
    p, idx, d = prices()
    adj = ev.add_abnormal(p, idx, 'market_adjusted').iloc[d]
    mm = ev.add_abnormal(p, idx, 'market_model').iloc[d]
    assert adj['z'] <= -1.96                                # v3: 베타 1 가정이라 급락으로 잡힌다
    assert mm['z'] > -1.96 and abs(mm['beta'] - 2.0) < 0.2  # v3.1: 시장 영향을 베타만큼 빼서 급락이 아니다


def test_common_stock_and_administrative_filters():
    row = {'corp_action': False, 'after_halt': False, 'mktcap_prev': 2000e8, 'stock_code': '000015',
           'company_name': 'A사우', 'sect': ''}
    assert 'not_common_stock' in ev.exclusion_reason(row, True, common_only=True)
    row2 = {**row, 'stock_code': '000010', 'company_name': 'A사', 'sect': '관리종목(소속부없음)'}
    assert ev.exclusion_reason(row2, True, common_only=True) == 'administrative_issue'
    assert ev.exclusion_reason({**row2, 'sect': '우량기업부'}, True, common_only=True) == ''
    assert ev.exclusion_reason(row, True) == ''             # 기존 표본(v3)은 그대로
