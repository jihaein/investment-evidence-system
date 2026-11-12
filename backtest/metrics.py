"""
metrics.py — 백테스트 평가지표 모음

backtest_01 노트북에서 만든 지표들을 한 곳에 모아 재사용한다.
자산가치 곡선 W_t (거래일별 포트폴리오 가치) 하나를 받아 숫자 하나를 돌려준다.
서로 다른 전략/회차가 모두 같은 정의를 쓰도록 하기 위함.
"""

import numpy as np
import pandas as pd

from backtest.engine import CASH_DAILY_RATE


def cumret(W):
    """누적수익률 = W_마지막 / W_처음 - 1."""
    return W.iloc[-1] / W.iloc[0] - 1


def mdd(W):
    """최대낙폭(MDD) = max(1 - W_t / 지금까지의 최고점)."""
    peak = W.cummax()
    return (1 - W / peak).max()


def sortino(W):
    """Sortino = √252 · 평균수익 / 하방편차. 하방편차=음(-)수익만의 RMS. 클수록 좋다."""
    r = W.pct_change().dropna()
    dd_down = (r.clip(upper=0) ** 2).mean() ** 0.5
    if dd_down == 0 or np.isnan(dd_down):
        return np.nan
    return (r.mean() / dd_down) * (252 ** 0.5)


def sharpe(W):
    """Sharpe = √252 · 평균수익 / 수익 표준편차 (Rf=0). 클수록 좋다."""
    r = W.pct_change().dropna()
    sd = r.std()
    if sd == 0 or np.isnan(sd):
        return np.nan
    return (r.mean() / sd) * (252 ** 0.5)


def cash_effect(grid, sell_schedule, end_date=None):
    out = {}
    for stock, sell_date in sell_schedule.items():
        future = grid.loc[sell_date:, stock]
        if end_date is not None:
            future = future.loc[:pd.Timestamp(end_date)]   # only measure within the backtest window
        future = future.dropna().iloc[1:]
        if len(future) == 0:
            continue
        R_hold = (1 + future / 100).prod() - 1
        R_cash = (1 + CASH_DAILY_RATE) ** len(future) - 1
        out[stock] = R_cash - R_hold
    return out


def cash_effect_summary(grid, sell_schedule, end_date=None):
    ce = cash_effect(grid, sell_schedule, end_date)
    if not ce:
        return {"n_sells": 0, "good": 0, "bad": 0, "avg_B": np.nan}
    good = sum(1 for b in ce.values() if b > 0)
    bad = sum(1 for b in ce.values() if b < 0)
    return {"n_sells": len(ce), "good": good, "bad": bad,
            "avg_B": sum(ce.values()) / len(ce)}

def scorecard(W):
    """한 전략의 W 곡선 → 4대 지표 묶음."""
    return {"누적수익률": cumret(W), "MDD": mdd(W),
            "Sortino": sortino(W), "Sharpe": sharpe(W)}
