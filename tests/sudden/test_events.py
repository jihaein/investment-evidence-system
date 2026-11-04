"""급락 사건 목록(models/sudden/events.py) 점검.
가짜 주가로 규칙(시장 조정, 과거만 쓰는 σ, 묶음, 제외, 판단 시각과 뉴스 창)을 확인하고,
저장소 data/의 실제 주가로 알려진 급락이 잡히는지 본다 (시장지수 없이 근사)."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "models" / "sudden"))
import events as E  # noqa: E402

DAYS = pd.bdate_range("2015-01-01", periods=320)                 # 주말을 뺀 거래일


def make(drops=None, index_drops=None, halt=(), corp_action=(), mktcap=500e8, name="가짜전자", code="000001"):
    rng = np.random.default_rng(3)
    ret = rng.normal(0, 0.02, len(DAYS))
    idx = rng.normal(0, 0.01, len(DAYS))
    for i, r in (drops or {}).items():
        ret[i] = r
    for i, r in (index_drops or {}).items():
        idx[i] = r
    vol = np.full(len(DAYS), 1000.0)
    for i in halt:
        ret[i], vol[i] = 0.0, 0.0
    close = 10000 * np.cumprod(1 + ret)
    for i in corp_action:                                         # 종가는 반토막인데 등락률은 평범 → 보정일
        close[i:] = close[i:] / 2
    prices = pd.DataFrame({"stock_code": code, "company_name": name, "market": "KOSDAQ", "trade_date": DAYS,
                           "close": close, "ret": ret, "volume": vol, "mktcap": mktcap})
    index = pd.DataFrame({"trade_date": DAYS, "market": "KOSDAQ", "index_ret": idx})
    return prices, index


def run(prices, index, **kw):
    return E.detect_events(prices, index, start="2015-01-01", end="2016-12-31", **kw)


def test_z_uses_only_the_120_days_before():
    prices, index = make()
    g = E.add_abnormal(prices, index)
    i = 250
    ar = (prices["ret"] - index["index_ret"]).to_numpy()
    assert np.isclose(g.loc[i, "z"], ar[i] / np.std(ar[i - 120:i], ddof=1))
    p2 = prices.copy()
    p2.loc[i + 1:, "ret"] = -0.5                                   # 뒤쪽을 바꿔도 i의 z는 그대로 (미래 정보 없음)
    assert np.isclose(E.add_abnormal(p2, index).loc[i, "z"], g.loc[i, "z"])


def test_market_wide_drop_is_not_an_event_but_a_stock_only_drop_is():
    prices, index = make(drops={200: -0.12}, index_drops={200: -0.12})
    ev, _, _ = run(prices, index)
    assert "000001_" + DAYS[200].strftime("%Y%m%d") not in set(ev.get("drop_id", []))
    prices, index = make(drops={200: -0.12})
    ev, _, _ = run(prices, index)
    assert "000001_" + DAYS[200].strftime("%Y%m%d") in set(ev["drop_id"])


def test_drops_within_10_trading_days_are_one_event():
    prices, index = make(drops={200: -0.15, 205: -0.15, 230: -0.15})
    ev, _, cand = run(prices, index)
    first = ev[ev["D"] == DAYS[200].strftime("%Y-%m-%d")].iloc[0]
    assert first["n_hit_days"] == 2 and first["last_hit_date"] == DAYS[205].strftime("%Y-%m-%d")
    assert DAYS[230].strftime("%Y-%m-%d") in set(ev["D"])
    assert cand.loc[cand["trade_date"] == DAYS[205].strftime("%Y-%m-%d"), "drop_id"].iloc[0] == first["drop_id"]


def test_corp_action_day_and_first_day_after_halt_are_excluded():
    prices, index = make(drops={200: -0.15, 251: -0.15}, halt=(248, 249, 250), corp_action=(200,))
    ev, _, cand = run(prices, index)
    reasons = dict(zip(cand["trade_date"], cand["excluded"]))
    assert reasons[DAYS[200].strftime("%Y-%m-%d")] == "corp_action_day"
    assert reasons[DAYS[251].strftime("%Y-%m-%d")] == "first_day_after_halt"
    assert not set(ev.get("D", [])) & {DAYS[200].strftime("%Y-%m-%d"), DAYS[251].strftime("%Y-%m-%d")}


def test_decision_time_news_window_and_sessions_cross_the_weekend():
    monday = next(i for i in range(200, 260) if DAYS[i].weekday() == 0)
    prices, index = make(drops={monday: -0.15})
    ev, ses, _ = run(prices, index)
    e = ev[ev["D"] == DAYS[monday].strftime("%Y-%m-%d")].iloc[0]
    assert e["decision_time"] == f"{DAYS[monday]:%Y-%m-%d}T15:30:00+09:00" == e["news_window_end"]
    assert e["news_window_start"] == f"{DAYS[monday - 3]:%Y-%m-%d}T15:30:00+09:00"    # D-3 거래일 = 지난주 수요일
    assert e["base_end"] == e["news_window_start"]                                       # 기준 기간과 뉴스 창이 겹치지 않음
    start = pd.Timestamp(DAYS[monday - 3]) + pd.Timedelta(hours=15, minutes=30)
    assert e["n_base_start"] == f"{start - pd.Timedelta(days=180):%Y-%m-%dT%H:%M:%S}+09:00"
    s = ses[ses["drop_id"] == e["drop_id"]].set_index("d")
    assert s.loc[0, "session_start"] == f"{DAYS[monday - 1]:%Y-%m-%d}T15:30:00+09:00"   # 금요일 15:30 초과 ~ 월요일 15:30
    assert s.loc[2, "session_start"] == e["news_window_start"]


def test_period_and_market_cap_rules():
    prices, index = make(drops={200: -0.15}, mktcap=500e8)
    ev, _, _ = E.detect_events(prices, index, start="2016-01-01", end="2016-12-31")
    assert DAYS[200].year == 2015 and DAYS[200].strftime("%Y-%m-%d") not in set(ev.get("D", []))   # 기간 밖
    ev, _, _ = run(prices, index)
    e = ev[ev["D"] == DAYS[200].strftime("%Y-%m-%d")].iloc[0]
    assert not bool(e["mktcap_ok"]) and e["mktcap_prev_eok"] == 500.0
    ev, _, cand = run(prices, index, apply_mktcap=True)
    assert DAYS[200].strftime("%Y-%m-%d") not in set(ev.get("D", []))
    assert cand.loc[cand["trade_date"] == DAYS[200].strftime("%Y-%m-%d"), "excluded"].iloc[0] == "mktcap_below_1000eok"


def test_index_csv_and_missing_market(tmp_path):
    prices, _ = make()
    path = tmp_path / "idx.csv"
    pd.DataFrame({"trade_date": DAYS, "market": "KOSDAQ", "close": np.linspace(600, 700, len(DAYS))}).to_csv(path, index=False)
    idx = E.load_index(prices, index_csv=path)
    closes = np.linspace(600, 700, len(DAYS))
    assert np.isclose(idx["index_ret"].iloc[1], closes[1] / closes[0] - 1)
    pd.DataFrame({"trade_date": DAYS, "market": "KOSPI", "close": 1.0}).to_csv(path, index=False)
    with pytest.raises(RuntimeError, match="KOSDAQ"):
        E.load_index(prices, index_csv=path)


def test_stock_names_keep_the_name_used_at_the_time():
    a, _ = make(name="옛이름")
    a.loc[160:, "company_name"] = "새이름"
    names = E.stock_names(a)
    assert list(names["company_name"]) == ["옛이름", "새이름"]
    assert names.iloc[1]["from"] == DAYS[160].strftime("%Y-%m-%d")


def test_real_data_known_drops_without_market_index():
    prices = E.load_prices()
    ev, _, _ = E.detect_events(prices, None)
    days = set(zip(ev["stock_code"], ev["D"]))
    for case in E.KNOWN_CASES:
        assert case in days
    assert ev["D"].min() >= "2016-01-01" and ev["D"].max() <= "2019-12-31"
    assert ev["market_adjusted"].eq(False).all()
    unjudged = E.unjudged_big_drops(E.add_abnormal(prices, None))
    assert ("052270", "2017-04-04") in set(zip(unjudged["stock_code"], unjudged["trade_date"]))   # 정리매매: σ 부족
    names = E.stock_names(prices)
    assert set(names.loc[names["stock_code"] == "050540", "company_name"]) == {"엠피씨", "한국코퍼레이션"}
