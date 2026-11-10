"""사후 주가 검증: 시장모형 CAR, 군집 t, 블록 순열검정을 가짜 데이터로 확인한다."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import backtest as bt


def fake_stock(beta=1.5, n=200, d=150, shock=-0.10, after=0.004):
    rng = np.random.default_rng(0)
    m = rng.normal(0, 0.01, n)
    r = 0.001 + beta * m + rng.normal(0, 0.001, n)
    r[d] = 0.001 + beta * m[d] + shock
    r[d + 1:] += after
    return pd.DataFrame({'ret': r, 'index_ret': m, 'halted': False}), d


def test_market_model_recovers_beta_and_car():
    stock, d = fake_stock()
    out = bt.market_model_car(stock, d, horizons=(20, 60))
    assert abs(out['beta'] - 1.5) < 0.05 and abs(out['AR_D'] + 0.10) < 0.005
    assert abs(out['CAR_20'] - 20 * 0.004) < 0.01 and abs(out['recovery_20'] - 0.8) < 0.1
    assert out['CAR_60'] is None or isinstance(out['CAR_60'], float)
    assert bt.market_model_car(stock, 30) is None                    # 추정 기간 부족


def test_clustered_t_matches_simple_difference():
    d, se, t, m = bt.clustered_t([0.0, 0.1, -0.1], [0.5, 0.6, 0.4], ['a', 'b', 'c'], ['d', 'e', 'f'])
    assert abs(d - 0.5) < 1e-9 and se > 0 and t > 3 and m == 6


def test_block_permutation_detects_clear_difference_and_not_noise():
    vals = [0.0] * 10 + [1.0] * 10
    labels = [0] * 10 + [1] * 10
    _, p = bt.block_permutation(vals, labels, [str(i) for i in range(20)], n=999)
    assert p < 0.01
    _, p_same = bt.block_permutation([0.5] * 20, labels, [str(i) for i in range(20)], n=199)
    assert p_same > 0.5
