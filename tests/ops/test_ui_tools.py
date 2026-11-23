"""ops/ui: packet → pipeline request mapping (analysis_worker) and price bars (push_prices)."""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops" / "ui"))
import analysis_worker as aw  # noqa: E402
import push_prices as pp  # noqa: E402

PACKET = json.loads((ROOT / "ops/ui/examples/packet_095270.json").read_text(encoding="utf-8"))


def test_packet_becomes_turn_one_request():
    r = aw.request_from_packet(PACKET, "2026-11-20")
    assert r == {"request_id": PACKET["request_id"], "session_id": PACKET["request_id"], "turn_id": 1,
                 "company_id": "095270", "alert_date": "2026-11-20", "conversation": PACKET["conversation"]}


@pytest.mark.parametrize("change,message", [({"company_id": None}, "company_id"), ({"conversation": []}, "answers"),
                                            ({"schema_version": "1.0"}, "schema")])
def test_bad_packets_are_rejected(change, message):
    with pytest.raises(ValueError, match=message):
        aw.request_from_packet({**PACKET, **change}, "2026-11-20")


def test_blank_answers_are_dropped():
    convo = [{"question": "q", "answer": "  "}, {"question": None, "answer": "이유가 있어"}]
    assert aw.request_from_packet({**PACKET, "conversation": convo}, "2026-11-20")["conversation"] == [convo[1]]


def test_alert_date_is_the_kst_day():
    assert aw.kst_date("2026-11-23T14:59:00Z") == "2026-11-23"
    assert aw.kst_date("2026-11-23T15:00:00+00:00") == "2026-11-24"


def test_halted_day_has_no_bar_and_numbers_are_parsed():
    row = {"code": "005930", "o": pp.num("71,000"), "h": pp.num("72,000"), "l": pp.num("70,500"),
           "c": pp.num("71,500"), "v": pp.num("12,345")}
    assert pp.bar(row, date(2026, 11, 20)) == {"code": "005930", "d": "2026-11-20", "o": 71000.0, "h": 72000.0,
                                               "l": 70500.0, "c": 71500.0, "v": 12345}
    assert pp.bar({**row, "o": pp.num("-")}, date(2026, 11, 20)) is None


import push_signals as ps  # noqa: E402


def test_real_holdings_skip_virtual_bad_and_duplicate():
    real = {"real": True}
    body = {"holdings": [{"code": "095270", "date": "2025-12-01", **real}, {"code": "095270", "date": "2025-12-01", **real},
                         {"code": "005930", "date": "2026-01-05", "virtual": True, **real}, {"name": "x", "date": "2026-01-01", **real},
                         {"code": "12345", "date": "2026-01-01", **real}, {"code": "018290", "date": "2026-13-01", **real},
                         {"code": "086520", "date": "2026-01-05"}]}                 # a mock tree with a code
    assert ps.real_holdings(body) == [("095270", date(2025, 12, 1))]


def test_rows_say_not_covered_no_signal_and_copy_the_verdict():
    today = date(2026, 11, 23)
    out = ps.to_row("p", "005930", date(2026, 1, 5), None, {"095270"}, today)
    assert out["status"] == "not_covered" and out["verdict"] is None
    assert ps.to_row("p", "095270", date(2019, 5, 1), {"error": "신호 없음"}, {"095270"}, today)["status"] == "no_signal"
    signal = {"status": "available", "verdict": "No", "fscore": 0, "ohlson_score": 0, "dupont_score": 0,
              "sum_total": 0, "eval_date": "2026-11-20", "baseline_date": "2025-09-01", "release_id": 17,
              "financial_as_of": "2026-11-17", "price_as_of": "2026-11-20", "rule_version": "seoseohi-sum-v1",
              "evidence": {}, "stock_name": "웨이브일렉트로"}
    row = ps.to_row("p", "095270", date(2025, 12, 1), signal, {"095270"}, today)
    assert row["status"] == "available" and row["verdict"] == "No" and row["release_id"] == 17
    assert "stock_name" not in row and row["buy_date"] == "2025-12-01"
