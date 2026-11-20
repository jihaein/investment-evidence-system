"""Convert receipt-verified OpenDART statements into immutable model snapshots."""
import calendar
import csv
import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import requests

from data_pipeline.fetch.fetch_disclosures import fetch, save_atomic
from seoseohi_preprocess.pipeline import Engine, Q, iso, read_csv, write_csv

REPORTS = {"사업보고서": "11011", "반기보고서": "11012", "분기보고서": None}
REPORT_TITLE = re.compile(r"(?:\[[^\]]+\]\s*)*(사업보고서|반기보고서|분기보고서)\s*\((\d{4})\.(\d{2})\)")
SOURCES = {
    "02": "FScore/02_재무_연간.csv",
    "09": "FScore/09_증자감자현황.csv",
    "04": "Ohlson/04_재무_Ohlson용.csv",
    "11": "DuPont/11_재무_DuPont_TTM.csv",
    "11B": "DuPont/11B_DuPont_날짜연결.csv",
}
LTD = "FScore/추가자료/30_총장기부채_합계확인상태.csv"
F7 = "FScore/추가자료/F7_보통주유상증자_연도별판정.csv"
SAME = "FScore/추가자료/FScore_동일기준_비교기간보완.csv"


class PendingReceipt(RuntimeError):
    """OpenDART has not made verifiable inputs available for this receipt."""


def json_file(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def receipt_id(value):
    if not re.fullmatch(r"[0-9]{14}", value or ""):
        raise ValueError("Invalid DART receipt ID")
    return value


def filing_metadata(entry, master_row):
    receipt_id(entry["rcept_no"])
    if "철" in entry.get("rm", "") or "[철회]" in entry["report_nm"]:
        raise PendingReceipt("withdrawal requires review before replacing the prior version")
    match = REPORT_TITLE.fullmatch(entry["report_nm"].strip())
    if not match:
        raise PendingReceipt("unrecognized periodic-report title")
    title, year, month = match.groups()
    year, month, closing = int(year), int(month), int(master_row["acc_mt"])
    if not 1 <= month <= 12 or not 1 <= closing <= 12:
        raise ValueError("Invalid filing period or fiscal closing month")
    distance = (closing - month) % 12
    report = REPORTS[title] or {9: "11013", 3: "11014"}.get(distance)
    if report is None or distance != {1: 9, 2: 6, 3: 3, 4: 0}[Q[report]]:
        raise PendingReceipt("report title does not match the fiscal calendar")
    fiscal_year = year + (month > closing)
    period = date(year, month, calendar.monthrange(year, month)[1])
    filed = date.fromisoformat(entry["rcept_dt"])
    if period >= filed:
        raise PendingReceipt("filing date does not follow the stated period")
    return {"corp_code": entry["corp_code"], "bsns_year": fiscal_year, "fiscal_year": fiscal_year,
            "reprt_code": report, "disclosed_date": filed.isoformat(), "period_end": period.isoformat()}


def store_json(path, payload):
    save_atomic(path, json.dumps(payload, ensure_ascii=False, sort_keys=True).encode())


def filing_payload(base, client, key, receipt, info, legacy_metadata):
    """Read or acquire only the requested version. DART's statement endpoint returns the latest."""
    raw = base / "_raw_pull" / "dart"
    prefix = f"{info['corp_code']}_{info['bsns_year']}_{info['reprt_code']}"
    archive = raw / "versions" / receipt
    manifest = archive / "filing.json"
    previous = json_file(manifest) if manifest.is_file() else {}
    if previous and any(previous.get(key) != value for key, value in info.items()):
        raise PendingReceipt("conflicting filing identity for an archived receipt")

    def get(basis):
        saved = archive / f"{prefix}_{basis}.json"
        legacy = raw / "fnlttSinglAcntAll" / saved.name
        for path in (saved, legacy):
            if path.is_file():
                payload = json_file(path)
                if payload.get("status") == "000" and {
                    row.get("rcept_no") for row in payload.get("list", [])
                } == {receipt}:
                    return payload
        if basis == "CFS" and (previous.get("cfs_absent_verified") or (
            client is None and legacy_metadata.get(receipt, {}).get("cfs_absent_verified")
        )):
            return {"status": "013"}
        if client is None:
            return {}
        return fetch(client, "fnlttSinglAcntAll.json", key, {
            "corp_code": info["corp_code"], "bsns_year": info["bsns_year"],
            "reprt_code": info["reprt_code"], "fs_div": basis,
        }).json()

    cfs = get("CFS")
    basis = "OFS" if cfs.get("status") == "013" else "CFS"
    payload = cfs if basis == "CFS" else get(basis)
    if payload.get("status") != "000" or {
        row.get("rcept_no") for row in payload.get("list", [])
    } != {receipt}:
        raise PendingReceipt("receipt-specific statement unavailable or replaced at the provider")
    record = dict(info, cfs_absent_verified=basis == "OFS")
    path = archive / f"{prefix}_{basis}.json"
    if not path.exists():
        store_json(path, payload)
    elif json_file(path) != payload:
        raise PendingReceipt("different statement content for the same receipt")
    store_json(archive / "filing.json", record)

    # F7 is only relevant for annual filings and requires an exact issuance table.
    if info["reprt_code"] == "11011":
        target = archive / "issuance.json"
        old = raw / "irdsSttus" / f"{prefix}.json"
        if not target.exists():
            issuance = json_file(old) if old.is_file() else {}
            if not exact_issuance(issuance, receipt) and client is not None:
                issuance = fetch(client, "irdsSttus.json", key, {
                    k: info[k] for k in ("corp_code", "bsns_year", "reprt_code")
                }).json()
                if issuance.get("status") not in {"000", "013"}:
                    raise PendingReceipt(f"issuance table request failed ({issuance.get('status')})")
            if exact_issuance(issuance, receipt):
                store_json(target, issuance)
    return path, record


def exact_issuance(payload, receipt):
    return payload.get("status") == "000" and {
        row.get("rcept_no") for row in payload.get("list", [])
    } == {receipt}


def source_history(base, corp, end, metadata):
    dart = base / "_raw_pull" / "dart"
    files = list((dart / "fnlttSinglAcntAll").rglob(f"{corp}_*.json"))
    if (dart / "versions").exists():
        files += list((dart / "versions").glob(f"*/{corp}_*.json"))
    for path in sorted(files):
        payload = json_file(path)
        if payload.get("status") != "000" or not payload.get("list"):
            continue
        receipt = payload["list"][0].get("rcept_no")
        archived = path.parent / "filing.json"
        record = json_file(archived) if archived.is_file() else metadata.get(receipt)
        if not record or record["disclosed_date"] >= end:
            continue
        if path.stem.endswith("_OFS") and not record.get("cfs_absent_verified"):
            # The same-receipt CFS is preferred. Unverified absence never permits OFS.
            continue
        yield path, receipt, dict(record, required=False)


def normalize(base, inputs, effective, sessions, metadata):
    """Use existing model preprocessing only for the affected companies and periods."""
    by_corp = {}
    for receipt, (path, record) in inputs.items():
        by_corp.setdefault(record["corp_code"], {})[receipt] = (path, record)
    output = {key: [] for key in ("02", "04", "11", "11B")}
    review, lineage = [], []
    for corp, changes in by_corp.items():
        history = list(source_history(base, corp, effective, metadata["filings"]))
        history += [(path, receipt, record) for receipt, (path, record) in changes.items()]
        directory = Engine(base, strict=True, metadata={
            "filings": {receipt: dict(record, required=receipt in changes)
                        for _, receipt, record in history},
            "trading_days": [day.isoformat() for day in sessions],
            "verified_facts": [fact for fact in metadata.get("verified_facts", [])
                               if fact["rcept_no"] in {item[1] for item in history}],
        }, dependency_as_of=(date.fromisoformat(effective) - timedelta(days=1)).isoformat())
        seen = {}
        for path, _, _ in history:
            company, year, report, basis = path.stem.split("_")
            directory.load_file(path, company, year, report, basis, seen)
        directory.finish_load(read_market=False)
        periods = {}
        for report in directory.reports:
            key = report["fy"], report["q"]
            periods[key] = max(report, periods.get(key, report), key=lambda r: (r["date"], r["rec"]))
        for report in periods.values():
            if report["rec"] in changes or any(
                change["fiscal_year"] * 4 + Q[change["reprt_code"]] <
                report["fy"] * 4 + report["q"] <= change["fiscal_year"] * 4 + Q[change["reprt_code"]] + 4
                for _, change in changes.values()
            ):
                directory.build_report(report)
        direct = set(changes)
        dependent = {item["rcept_no"] for item in directory.lineage
                     if direct.intersection(item["operand_receipts"].split("|"))}
        for key in output:
            output[key] += [row for row in directory.out[key] if row["rcept_no"] in direct | dependent]
        review += directory.review
        lineage += directory.lineage
    return output, review, lineage


def overlay(path, target, additions, keys, *, two=True, remove=None):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        labels = next(reader) if two else None
    rows = read_csv(path, two=two)
    changed = {tuple(str(row[key]) for key in keys) for row in additions}
    remaining = [row for row in rows if tuple(row[key] for key in keys) not in changed
                 and not (remove and remove(row))]
    write_csv(target, remaining + additions, header, labels)


def annual_supplements(base, annual, effective, period_end):
    current = annual["thstrm_amount"]
    prior = annual.get("frmtrm_amount", {})
    receipt = current["rcept_no"]
    identity = {key: current[key] for key in ("stock_code", "stock_name_2014", "fiscal_year")}
    debt = dict(identity, fs_div=current["fs_div"], source_receipt=receipt, total_long_term_debt=current["SUM"],
                total_long_term_debt_prior=prior.get("SUM", ""), source_evidence=f"DART receipt {receipt}",
                status="RECEIPT_RECONCILED" if current["SUM"] and prior.get("SUM") else "REVIEW_REQUIRED")
    comparisons = []
    for slot in ("frmtrm_amount", "bfefrmtrm_amount"):
        past = annual.get(slot, {})
        for column in ("ifrs_Assets", "ifrs_ProfitLoss", "ifrs_Revenue", "ifrs_GrossProfit",
                       "ifrs_CurrentAssets", "ifrs_CurrentLiabilities"):
            if past.get(column) and (slot == "frmtrm_amount" or column == "ifrs_Assets"):
                comparisons.append(dict(identity, fs_div=current["fs_div"], score_year=current["fiscal_year"],
                                        value_fiscal_year=past["fiscal_year"], column=column, amount=past[column],
                                        source_receipt=receipt, amount_column=slot, reason="DART_SAME_REPORT"))
    archive = base / "_raw_pull/dart/versions" / receipt / "issuance.json"
    complete = False
    issued = False
    rows = []
    if archive.is_file():
        payload = json_file(archive)
        if exact_issuance(payload, receipt) and all(
            item.get("corp_code") == current["corp_code"] for item in payload["list"]
        ):
            rows = payload["list"]
        complete = bool(rows) and all(item.get("stlm_dt") == period_end for item in rows)
        end = date.fromisoformat(period_end)
        start = date(end.year - 1, end.month, min(end.day, calendar.monthrange(end.year - 1, end.month)[1]))
        for item in rows:
            kind = item.get("isu_dcrs_stock_knd", "").strip()
            event = item.get("isu_dcrs_stle", "").strip()
            if not event or "유상증자" not in event:
                complete &= bool(event and event != "-")
                continue
            try:
                day = date.fromisoformat(iso(item["isu_dcrs_de"].replace(".", "").replace(" ", "")))
                quantity = int(item["isu_dcrs_qy"].replace(",", ""))
            except (ValueError, KeyError):
                complete = False
                continue
            if not kind or quantity <= 0:
                complete = False
                continue
            issued |= start < day <= end and kind == "보통주"
    score = 0 if issued else 1 if complete else ""
    f7 = dict(identity, source_receipts=receipt, evidence_cutoff=effective, F7_score=score,
              common_equity_issued=str(issued) if score != "" else "",
              status=("ACTUAL_COMMON_PAID_ISSUANCE_IN_SELECTED_REPORT" if issued else
                      "NO_COMMON_PAID_ISSUANCE_IN_COMPLETE_REPORT_TABLE" if complete else "ISSUANCE_TABLE_NOT_IDENTIFIED"),
              source_evidence=f"DART receipt {receipt}")
    issuance = [dict(row, stock_code=current["stock_code"], bsns_year=current["bsns_year"], reprt_code="11011")
                for row in rows]
    return debt, f7, comparisons, issuance


def write_snapshot(base, seed, target, inputs, effective, sessions, metadata, observed):
    effective = effective.isoformat()
    outputs, review, lineage = normalize(base, inputs, effective, sessions, metadata)
    required = set(inputs)
    if not all(required <= {row["rcept_no"] for row in outputs[key]} for key in ("04", "11", "11B")):
        raise PendingReceipt("normalized snapshot would omit one or more required statements")
    annual = {}
    debt, f7, comparisons, issuance = [], [], [], []
    for row in outputs["02"]:
        if row["rcept_no"] in required:
            annual.setdefault(row["rcept_no"], {})[row["amount_column"]] = row
    for receipt, group in annual.items():
        annual_debt, annual_f7, annual_comparisons, issues = annual_supplements(
            base, group, effective, inputs[receipt][1]["period_end"])
        if annual_f7["F7_score"] == "":
            review.append({"rcept_no": receipt, "column": "F7_score", "reason": "ISSUANCE_TABLE_NOT_IDENTIFIED"})
        debt.append(annual_debt)
        f7.append(annual_f7)
        comparisons.extend(annual_comparisons)
        issuance.extend(issues)
    annual_keys = {(row["stock_code"], str(row["fiscal_year"])) for row in debt}
    fields = ("stock_code", "bsns_year", "quarter")

    def period_key(row):
        return tuple(str(row[field]) for field in fields)

    inherited = {key: {period_key(row): row["rcept_no"] for row in read_csv(seed / SOURCES[key], two=True)}
                 for key in ("04", "11", "11B")}
    eligible_dependencies = {period_key(row) for row in outputs["04"]
                             if all(periods.get(period_key(row)) == row["rcept_no"]
                                    for periods in inherited.values())}
    changed_periods = {period_key(row): row["rcept_no"] for row in outputs["04"]
                       if row["rcept_no"] in required or period_key(row) in eligible_dependencies}
    for key in inherited:
        additions = [row for row in outputs[key] if period_key(row) in changed_periods]
        overlay(seed / SOURCES[key], target / SOURCES[key], additions, fields)
    overlay(seed / SOURCES["02"], target / SOURCES["02"],
            [group["thstrm_amount"] for group in annual.values()], ("stock_code", "fiscal_year"))
    overlay(seed / SOURCES["09"], target / SOURCES["09"], issuance, ("stock_code", "bsns_year", "reprt_code"),
            remove=lambda row: row["reprt_code"] == "11011" and (row["stock_code"], row["bsns_year"]) in annual_keys)
    overlay(seed / LTD, target / LTD, debt, ("stock_code", "fiscal_year"), two=False)
    overlay(seed / F7, target / F7, f7, ("stock_code", "fiscal_year"), two=False)
    overlay(seed / SAME, target / SAME, comparisons, ("stock_code", "score_year", "value_fiscal_year", "column"),
            two=False, remove=lambda row: (row["stock_code"], row["score_year"]) in annual_keys)

    for key in inherited:
        rows = read_csv(target / SOURCES[key], two=True)
        periods = {period_key(row): row["rcept_no"] for row in rows}
        if len(periods) != len(rows) or any(periods.get(period) != receipt
                                           for period, receipt in changed_periods.items()):
            raise ValueError(f"Unmatched company-period or receipt in {SOURCES[key]}")
    if not annual_keys <= {(row["stock_code"], row["fiscal_year"])
                           for row in read_csv(target / SOURCES["02"], two=True)}:
        raise ValueError("Annual statement missing from F-Score snapshot")
    store_json(target / "review.json", review)
    store_json(target / "lineage.json", lineage)
    store_json(target / "approved.json", {
        "effective_session": effective,
        "approved_by": "automated-dart-validation-v1",
        "receipts": sorted(required),
        "observed_receipts": sorted(observed),
        "sha256": {name: hashlib.sha256((target / name).read_bytes()).hexdigest()
                   for name in (*SOURCES.values(), LTD, F7, SAME)},
    })


def version_metadata(base):
    path = base / "_raw_pull" / "preprocess_metadata.json"
    return json_file(path) if path.is_file() else {"filings": {}}


def new_snapshot(base, seed, target, entries, effective, sessions, *, key=None):
    """Build an eligible batch and return receipt-level issues for the next retry."""
    metadata = version_metadata(base)
    companies = {row["corp_code"]: row for row in read_csv(base / "12_기업개황_KSIC.csv", two=True)}
    errors = {}
    groups = {}
    current = {(row["corp_code"], row["bsns_year"], row["quarter"]):
               (date.fromisoformat(row["disclosed_date"]), row["rcept_no"])
               for row in read_csv(seed / SOURCES["04"], two=True)}
    for entry in entries:
        receipt = receipt_id(entry["rcept_no"])
        if entry["corp_code"] not in companies:
            continue
        try:
            record = filing_metadata(entry, companies[entry["corp_code"]])
        except (PendingReceipt, ValueError, KeyError) as exc:
            errors[receipt] = str(exc)
            continue
        period = record["corp_code"], str(record["bsns_year"]), str(Q[record["reprt_code"]])
        disclosed = date.fromisoformat(record["disclosed_date"])
        if current.get(period, (date.min, "")) >= (disclosed, receipt):
            continue
        groups.setdefault(period, []).append((disclosed, receipt, record))
    inputs, observed = {}, set()
    with requests.Session() as client:
        for versions in groups.values():
            _, receipt, record = max(versions)
            try:
                inputs[receipt] = filing_payload(base, client if key else None, key, receipt,
                                                 record, metadata["filings"])
            except (PendingReceipt, ValueError, KeyError, RuntimeError) as exc:
                errors[receipt] = str(exc)
                continue
            observed.update(item[1] for item in versions)
    if inputs:
        target.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=target, prefix=".assembling-") as staging:
            folder = Path(staging) / "snapshot"
            write_snapshot(base, seed, folder, inputs, effective, sessions, metadata, observed)
            digest = hashlib.sha256((folder / "approved.json").read_bytes()).hexdigest()[:20]
            final = target / f"{effective}-{digest}"
            if not final.exists():
                folder.rename(final)
        return final, errors
    return None, errors
