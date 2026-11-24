"""data_pipeline/scheduled_refresh.py triage_pending: which held DART receipts alert a person."""
import json
from datetime import date

from data_pipeline import scheduled_refresh as sr


def receipt(inbox, no, corp, stock, filed):
    (inbox / f"{no}.json").write_text(json.dumps({"rcept_no": no, "corp_code": corp, "stock_code": stock,
                                                  "corp_name": "x", "report_nm": "분기보고서", "rcept_dt": filed}))


def test_old_held_receipt_alerts_until_a_person_lists_it(tmp_path, monkeypatch):
    inbox = tmp_path / "receipts"; inbox.mkdir()
    receipt(inbox, "20261113000176", "c1", "030960", "2026-11-13")
    receipt(inbox, "20261120000001", "c2", "099750", "2026-11-20")
    monkeypatch.delenv("MARKET_COVERAGE_CSV", raising=False)
    monkeypatch.setenv("DART_REVIEWED_CSV", str(tmp_path / "none.csv"))
    pending = {"20261113000176": "statement unavailable", "20261120000001": "statement unavailable"}
    t = sr.triage_pending(tmp_path, inbox, pending, set(), date(2026, 11, 24))
    assert t["20261113000176"]["status"] == "needs_review"      # 11 days: past RETRY_DAYS
    assert t["20261120000001"]["status"] == "retrying"
    reviewed = tmp_path / "reviewed.csv"
    reviewed.write_text("rcept_no,stock_code,corp_name,reviewed_on,note\n20261113000176,030960,양지사,2026-11-24,x\n",
                        encoding="utf-8")
    monkeypatch.setenv("DART_REVIEWED_CSV", str(reviewed))
    t = sr.triage_pending(tmp_path, inbox, pending, set(), date(2026, 11, 24))
    assert t["20261113000176"]["status"] == "reviewed"


def test_repo_list_parses():
    assert "20261113000176" in sr.reviewed_receipts(sr.REVIEWED_CSV)
