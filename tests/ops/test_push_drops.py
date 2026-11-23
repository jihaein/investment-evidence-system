"""ops/ui/push_drops.py: the daily 급락 감지 on DB rows (no 등락률, no volume), with the rule of models/sudden/events.py."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops" / "ui"))
import push_drops as pd_  # noqa: E402


def sessions(n, start="2026-01-05"):
    return pd.bdate_range(start, periods=n)


def db_rows(code, days, closes, shares=None, market="코스닥"):
    shares = shares if shares is not None else [1_000_000] * len(days)
    return pd.DataFrame({"stock_code": code, "stock_name": f"회사{code}", "market": market,
                         "bas_dd": [d.date() for d in days], "close_prc": closes,
                         "mktcap": [c * s for c, s in zip(closes, shares)], "list_shrs": shares})


def walk(n, seed, daily=0.01, start=10000.0):
    """a calm series: small moves whose z never reaches -1.96 on its own (a random walk would, ~2.5% of days)"""
    rng = np.random.default_rng(seed)
    moves = rng.uniform(-daily, daily, n)
    return list(start * np.cumprod(1 + moves))


def flat_index(days, market="KOSDAQ"):
    return pd_.index_returns(pd.DataFrame({"trade_date": days, "market": market, "close": 1000.0}))


def test_a_sharp_drop_against_a_flat_market_is_an_event():
    days = sessions(160)
    closes = walk(159, 1) ; closes.append(closes[-1] * 0.85)          # the last day: -15% against a flat index
    prices = pd_.prepare(db_rows("095270", days, closes), days)
    found, _ = pd_.detect(prices, flat_index(days), days[130], days[-1])
    assert list(found["stock_code"]) == ["095270"]
    assert found.iloc[0]["D"] == days[-1].strftime("%Y-%m-%d")
    assert found.iloc[0]["z"] <= -1.96
    assert abs(found.iloc[0]["ret_pct"] + 15) < 0.01
    rows = pd_.to_rows(found, True)
    assert rows[0]["code"] == "095270" and rows[0]["market_adjusted"] is True and rows[0]["rule_version"]


def test_the_market_falling_as_much_is_not_a_drop():
    days = sessions(160)
    closes = walk(159, 2) ; closes.append(closes[-1] * 0.85)
    index = pd.DataFrame({"trade_date": days, "market": "KOSDAQ", "close": [1000.0] * 159 + [850.0]})
    found, _ = pd_.detect(pd_.prepare(db_rows("095270", days, closes), days), pd_.index_returns(index), days[130], days[-1])
    assert found.empty


def test_a_share_count_change_day_is_not_judged():
    days = sessions(160)
    closes = walk(159, 3) ; closes.append(closes[-1] * 0.5)            # a 2:1 split looks like -50%
    shares = [1_000_000] * 159 + [2_000_000]
    found, cand = pd_.detect(pd_.prepare(db_rows("095270", days, closes, shares), days), flat_index(days),
                             days[130], days[-1])
    assert found.empty
    assert list(cand["excluded"]) == ["corp_action_day"]


def test_a_missing_session_is_a_halt_and_the_day_after_is_not_judged():
    days = sessions(160)
    closes = walk(160, 4)
    closes[-1] = closes[-3] * 0.8                                       # back after a halt, 20% lower
    rows = db_rows("095270", days, closes).drop(index=158)              # session 158 missing
    prices = pd_.prepare(rows, days)
    assert len(prices) == 160 and prices.iloc[158]["volume"] == 0
    found, cand = pd_.detect(prices, flat_index(days), days[130], days[-1])
    assert found.empty
    assert "first_day_after_halt" in set(cand["excluded"])


def test_drops_within_ten_sessions_are_one_event():
    days = sessions(170)
    closes = walk(170, 5)
    for i in (150, 155):
        closes[i:] = [c * 0.85 for c in closes[i:]]
    found, _ = pd_.detect(pd_.prepare(db_rows("095270", days, closes), days), flat_index(days), days[130], days[-1])
    assert len(found) == 1 and found.iloc[0]["n_hit_days"] == 2
    assert found.iloc[0]["last_hit_date"] == days[155].strftime("%Y-%m-%d")


def test_too_little_history_is_not_judged():
    days = sessions(60)
    closes = walk(59, 6) ; closes.append(closes[-1] * 0.7)
    found, _ = pd_.detect(pd_.prepare(db_rows("095270", days, closes), days), flat_index(days), days[0], days[-1])
    assert found.empty


def test_a_move_beyond_the_price_limit_is_not_judged():
    days = sessions(160)
    closes = walk(159, 7) ; closes.append(closes[-1] * 0.05)           # -95%: a base-price reset, not trading
    found, cand = pd_.detect(pd_.prepare(db_rows("096610", days, closes), days), flat_index(days), days[130], days[-1])
    assert found.empty and list(cand["excluded"]) == ["corp_action_day"]


def test_limit_down_itself_is_still_a_drop():
    days = sessions(160)
    closes = walk(159, 8) ; closes.append(closes[-1] * 0.70)           # -30.0%: 하한가
    found, _ = pd_.detect(pd_.prepare(db_rows("008290", days, closes), days), flat_index(days), days[130], days[-1])
    assert list(found["stock_code"]) == ["008290"]
