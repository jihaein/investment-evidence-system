"""ops/ui/push_drops.py: the daily 급락 감지 on DB rows + each session's 등락률, with the rule of models/sudden/events.py."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops" / "ui"))
import push_drops as pd_  # noqa: E402


def sessions(n, start="2026-01-05"):
    return pd.bdate_range(start, periods=n)


def db_rows(code, days, closes, fluc=None, volume=None, market="코스닥"):
    """DB rows joined with 등락률: by default the 등락률 is the close return (no corporate action)."""
    s = pd.Series(closes, dtype=float)
    fluc = list(fluc) if fluc is not None else list((s.pct_change() * 100).fillna(0))
    volume = volume if volume is not None else [1000] * len(days)
    return pd.DataFrame({"stock_code": code, "stock_name": f"회사{code}", "market": market,
                         "bas_dd": [d.date() for d in days], "close_prc": closes, "mktcap": [c * 1e6 for c in closes],
                         "list_shrs": 1_000_000, "fluc": fluc, "volume": volume})


def walk(n, seed, daily=0.01, start=10000.0):
    """a calm series: small moves whose z never reaches -1.96 on its own (a random walk would, ~2.5% of days)"""
    rng = np.random.default_rng(seed)
    return list(start * np.cumprod(1 + rng.uniform(-daily, daily, n)))


def flat_index(days, market="KOSDAQ"):
    return pd_.index_returns(pd.DataFrame({"trade_date": days, "market": market, "close": 1000.0}))


def run(rows, days, index=None, start=130):
    return pd_.detect(pd_.prepare(rows, days), index if index is not None else flat_index(days), days[start], days[-1])


def test_a_sharp_drop_against_a_flat_market_is_an_event():
    days = sessions(160)
    closes = walk(159, 1) ; closes.append(closes[-1] * 0.85)
    found, _ = run(db_rows("095270", days, closes), days)
    assert list(found["stock_code"]) == ["095270"]
    assert found.iloc[0]["D"] == days[-1].strftime("%Y-%m-%d") and found.iloc[0]["z"] <= -1.96
    assert abs(found.iloc[0]["ret_pct"] + 15) < 0.01
    rows = pd_.to_rows(found, True)
    assert rows[0]["code"] == "095270" and rows[0]["market_adjusted"] is True and rows[0]["rule_version"]


def test_the_market_falling_as_much_is_not_a_drop():
    days = sessions(160)
    closes = walk(159, 2) ; closes.append(closes[-1] * 0.85)
    index = pd.DataFrame({"trade_date": days, "market": "KOSDAQ", "close": [1000.0] * 159 + [850.0]})
    found, _ = run(db_rows("095270", days, closes), days, pd_.index_returns(index))
    assert found.empty


def test_an_ex_dividend_day_is_not_judged():
    # 038070 2018-12-27 in the research file: close -6.45%, 등락률 -1.46% (the base price was adjusted)
    days = sessions(160)
    closes = walk(159, 3) ; closes.append(closes[-1] * 0.9355)
    fluc = list((pd.Series(closes).pct_change() * 100).fillna(0)) ; fluc[-1] = -1.46
    found, cand = run(db_rows("038070", days, closes, fluc=fluc), days)
    assert found.empty
    assert list(cand["excluded"]) in ([], ["corp_action_day"])


def test_a_split_day_is_not_judged():
    days = sessions(160)
    closes = walk(159, 4) ; closes.append(closes[-1] * 0.2)            # 5:1 split: close -80%, 등락률 +1%
    fluc = list((pd.Series(closes).pct_change() * 100).fillna(0)) ; fluc[-1] = 1.0
    found, _ = run(db_rows("096350", days, closes, fluc=fluc), days)
    assert found.empty


def test_a_missing_session_is_a_halt_and_the_day_after_is_not_judged():
    days = sessions(160)
    closes = walk(160, 5)
    closes[-1] = closes[-3] * 0.8
    fluc = list((pd.Series(closes).pct_change() * 100).fillna(0)) ; fluc[-1] = -20.0
    rows = db_rows("095270", days, closes, fluc=fluc).drop(index=158)
    prices = pd_.prepare(rows, days)
    assert len(prices) == 160 and prices.iloc[158]["volume"] == 0
    found, cand = pd_.detect(prices, flat_index(days), days[130], days[-1])
    assert found.empty and "first_day_after_halt" in set(cand["excluded"])


def test_a_zero_volume_row_is_a_halt_too():
    days = sessions(160)
    closes = walk(160, 6)
    closes[-1] = closes[-2] * 0.8
    vol = [1000] * 160 ; vol[158] = 0
    found, cand = run(db_rows("095270", days, closes, volume=vol), days)
    assert found.empty and "first_day_after_halt" in set(cand["excluded"])


def test_drops_within_ten_sessions_are_one_event():
    days = sessions(170)
    closes = walk(170, 7)
    for i in (150, 155):
        closes[i:] = [c * 0.85 for c in closes[i:]]
    found, _ = run(db_rows("095270", days, closes), days)
    assert len(found) == 1 and found.iloc[0]["n_hit_days"] == 2
    assert found.iloc[0]["last_hit_date"] == days[155].strftime("%Y-%m-%d")


def test_too_little_history_is_not_judged():
    days = sessions(60)
    closes = walk(59, 8) ; closes.append(closes[-1] * 0.7)
    found, _ = run(db_rows("095270", days, closes), days, start=0)
    assert found.empty


def test_session_fluc_is_fetched_once_and_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(pd_, "FLUC_DIR", tmp_path)
    monkeypatch.setattr(pd_, "FETCH_PAUSE", 0)
    calls = []
    def fetch(tag, market):
        calls.append((tag, market))
        return pd.DataFrame({"등락률": [-3.5, 1.2], "거래량": [100, 0]}, index=pd.Index(["095270", "000001"], name="티커"))
    day = pd.Timestamp("2026-11-20")
    a = pd_.session_fluc(day, ["KOSDAQ"], fetch)
    b = pd_.session_fluc(day, ["KOSDAQ"], fetch)
    assert calls == [("20261120", "ALL")]
    assert a.equals(b) and list(a["stock_code"]) == ["095270", "000001"]
    raw = pd.DataFrame({"stock_code": ["095270"], "stock_name": ["웨이브일렉트로"], "market": ["KOSDAQ"], "bas_dd": [day.date()],
                        "close_prc": [5000], "mktcap": [1], "list_shrs": [1]})
    joined = pd_.with_fluc(raw, [day], ["KOSDAQ"], fetch)
    assert joined.loc[0, "fluc"] == -3.5 and joined.loc[0, "volume"] == 100


def test_a_failed_session_says_which_day(tmp_path, monkeypatch):
    monkeypatch.setattr(pd_, "FLUC_DIR", tmp_path)
    monkeypatch.setattr(pd_, "FETCH_PAUSE", 0)
    def fetch(tag, market):
        raise KeyError("None of ['시가'] are in the [columns]")
    try:
        pd_.session_fluc(pd.Timestamp("2026-11-20"), None, fetch)
        assert False
    except RuntimeError as e:
        assert "2026-11-20" in str(e)
    assert not list(tmp_path.iterdir())                       # nothing cached for a failed day
