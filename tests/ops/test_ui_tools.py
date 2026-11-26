"""ops/ui: packet → pipeline request mapping (analysis_worker) and price bars (push_prices)."""
import json
import sys
import subprocess
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


class FakeSupa:
    """price_daily as a dict code → sorted days; records upserts."""
    def __init__(self, days):
        self.days, self.up = days, []

    def req(self, method, path, body=None, prefer=None):
        code = path.split("code=eq.")[1].split("&")[0]
        ds = sorted(self.days.get(code, []), reverse=True)
        return [{"d": d} for d in ds[:1]]

    def upsert(self, table, rows, on):
        self.up += rows


def test_indexes_start_at_their_last_day_and_never_pass_the_stocks(monkeypatch):
    import pandas as pd
    asked = []

    def fake(code, start, end):
        asked.append((code, start, end))
        days = pd.bdate_range(start, end + pd.Timedelta(days=3))           # a source that runs ahead of the stocks
        return [{"code": code, "d": d.strftime("%Y-%m-%d"), "o": 1, "h": 1, "l": 1, "c": 1, "v": 0} for d in days]
    monkeypatch.setattr(pp, "index_bars", fake)
    monkeypatch.setattr(pp.time, "sleep", lambda s: None)
    sb = FakeSupa({"KOSDAQ": ["2026-11-19"]})
    pp.push_indexes(sb, date(2024, 1, 1), date(2026, 11, 20))
    assert ("KOSDAQ", date(2026, 11, 19), date(2026, 11, 20)) in asked        # refetch its last day
    assert ("KOSPI", date(2024, 1, 1), date(2026, 11, 20)) in asked           # never uploaded: from --from
    assert max(r["d"] for r in sb.up) == "2026-11-20"


def test_a_halted_stock_with_history_is_not_fetched_again():
    sb = FakeSupa({"031860": ["2026-04-03"]})
    assert pp.never_loaded(sb, {"031860", "999999"}) == ["999999"]


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


def test_alert_date_comes_from_the_packet_when_it_is_there():
    assert aw.alert_date_of({"alert_date": "2026-11-20"}, "2026-11-23T01:00:00Z") == "2026-11-20"
    assert aw.alert_date_of({}, "2026-11-22T16:30:00Z") == "2026-11-23"            # arrival day in KST
    assert aw.alert_date_of({"alert_date": "nope"}, "2026-11-22T14:00:00Z") == "2026-11-22"


# ── S10: the worker runs run_mixed_pipeline.py and stores 루온's line next to the Toulmin result

def test_stage_models_are_the_pipelines_own():
    models = aw.stage_models()
    assert set(models) == {"S4", "S5", "S7", "S10"} and all(isinstance(v, str) and v for v in models.values())


def _completed_run(tmp_path, line, validation):
    (tmp_path / "S9").mkdir()
    (tmp_path / "S10").mkdir()
    (tmp_path / "S9/toulmin.json").write_text(json.dumps({"decision": "mixed", "as_of": "2026-09-01"}), encoding="utf-8")
    (tmp_path / "S10/dialogue.json").write_text(json.dumps(line, ensure_ascii=False), encoding="utf-8")
    return {"status": "completed", "toulmin": str(tmp_path / "S9/toulmin.json"), "dialogue": str(tmp_path / "S10/dialogue.json"),
            "s10_validation": validation, "stage_models": {"S10": "gpt-6.2-sol"}, "total_runtime_seconds": 41.5,
            "estimated_cost_usd": 0.031}


def test_read_run_keeps_the_line_and_its_evidence(tmp_path):
    line = {"character_message": "매출액을 보면 2025년 4분기 약 246억 원이었어. 네 말처럼 더 좋아질지는 아직 몰라.",
            "emotion_code": "thinking", "asked_cc_id": "cc-1"}
    validation = {"fallback": False, "attempts": 1, "scope_case": "none",
                  "citations": {"[[F1]]": {"text": "…", "fact_ids": ["S3-b", "S3-a"]}}}
    result, dialogue = aw.read_run(_completed_run(tmp_path, line, validation))
    assert result["decision"] == "mixed"
    assert dialogue["character_message"] == line["character_message"] and dialogue["emotion_code"] == "thinking"
    assert dialogue["asked_cc_id"] == "cc-1" and dialogue["speaker"] == "luon" and dialogue["fallback"] is False
    assert dialogue["fact_ids"] == ["S3-a", "S3-b"] and dialogue["estimated_cost_usd"] == 0.031


def test_read_run_refuses_an_empty_line(tmp_path):
    with pytest.raises(RuntimeError):
        aw.read_run(_completed_run(tmp_path, {"character_message": " ", "emotion_code": "neutral", "asked_cc_id": None}, {}))


def test_run_pipeline_reports_the_pipelines_own_error(tmp_path, monkeypatch):
    def fake_run(argv, cwd, env, **_):
        assert argv[1] == "run_mixed_pipeline.py" and env["OPENAI_MODEL_S10"] == aw.stage_models()["S10"]
        out = Path(argv[argv.index("--output") + 1])
        out.mkdir()
        (out / "summary.json").write_text(json.dumps({"status": "failed", "error_type": "ValueError",
                                                      "error": "expected one effective company argument"}))
        return subprocess.CompletedProcess(argv, 1, "", "Traceback ...")
    monkeypatch.setattr(aw.subprocess, "run", fake_run)
    monkeypatch.delenv("OPENAI_MODEL_S10", raising=False)
    with pytest.raises(RuntimeError, match="ValueError: expected one effective"):
        aw.run_pipeline({"request_id": "r1"}, tmp_path)
