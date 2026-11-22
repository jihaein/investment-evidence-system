from datetime import date

import pytest

from data_pipeline import dart_conversion as conversion


@pytest.mark.parametrize(("name", "filed", "closing", "fiscal_year", "reprt", "period"), [
    ("분기보고서 (2026.09)", "20261113", "12", 2026, "11014", "2026-09-30"),
    ("[기재정정]분기보고서 (2025.09)", "20261113", "06", 2026, "11013", "2025-09-30"),
    ("사업보고서 (2025.12)", "20260312", "12", 2025, "11011", "2025-12-31"),
])
def test_period_identity_from_report_title(name, filed, closing, fiscal_year, reprt, period):
    meta = conversion.filing_metadata({
        "rcept_no": filed + "000001", "rcept_dt": filed, "corp_code": "00123456", "report_nm": name,
    }, {"acc_mt": closing})
    assert (meta["bsns_year"], meta["reprt_code"], meta["period_end"]) == (fiscal_year, reprt, period)


def test_never_archives_a_later_revision_as_the_requested_receipt(tmp_path, monkeypatch):
    target = "20260312000001"
    latest = "20260313000001"

    class Response:
        def json(self):
            return {"status": "000", "list": [{"rcept_no": latest}]}

    monkeypatch.setattr(conversion, "fetch", lambda *args: Response())
    with pytest.raises(conversion.PendingReceipt, match="receipt-specific"):
        conversion.filing_payload(tmp_path, object(), "test",
                                  target, {"corp_code": "00123456", "bsns_year": 2025, "reprt_code": "11011"}, {})
    assert not list(tmp_path.rglob("*.json"))


def test_f7_checks_receipt_stock_kind_and_fiscal_window(tmp_path):
    receipt = "20260312000001"
    current = {"stock_code": "010170", "stock_name_2014": "Test", "corp_code": "00123456",
               "rcept_no": receipt, "fs_div": "CFS", "bsns_year": "2025", "fiscal_year": "2025",
               "SUM": "100", "ifrs_Assets": "1200"}
    annual = {"thstrm_amount": current, "frmtrm_amount": {
        **current, "fiscal_year": "2024", "SUM": "95", "ifrs_Assets": "1000"}}
    args = (tmp_path, annual, date(2026, 3, 13).isoformat(), "2025-12-31")
    debt, issuance, comparison, raw = conversion.annual_supplements(*args)
    assert debt["total_long_term_debt_prior"] == "95"
    assert comparison[0]["source_receipt"] == receipt
    assert issuance["F7_score"] == ""
    assert not raw
    path = tmp_path / "_raw_pull/dart/versions" / receipt / "issuance.json"
    record = {"rcept_no": receipt, "corp_code": "00123456", "stlm_dt": "2025-12-31",
              "isu_dcrs_de": "2024.09.12", "isu_dcrs_stle": "유상증자(주주배정)",
              "isu_dcrs_stock_knd": "보통주", "isu_dcrs_qy": "10,000"}
    conversion.store_json(path, {"status": "000", "list": [record]})
    assert conversion.annual_supplements(*args)[1]["F7_score"] == 1
    conversion.store_json(path, {"status": "000", "list": [dict(record, isu_dcrs_de="2025.09.12")]})
    _, issuance, _, raw = conversion.annual_supplements(*args)
    assert issuance["F7_score"] == 0
    assert raw[0]["bsns_year"] == "2025" and raw[0]["reprt_code"] == "11011"


def test_same_year_amendment_is_new_information_for_pit_fscore():
    from models.seoseohi.fscore import _fill_scores

    original = {"fiscal_year": 2025, "rcept_no": "20260312000001", "f_score": 7}
    amended = dict(original, rcept_no="20260414000002", f_score=3)
    result = _fill_scores({}, original, amended, compare_revisions=True)
    assert result["same_report"] is False
    assert result["risk_score"] == 2
