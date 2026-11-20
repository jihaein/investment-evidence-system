"""Poll OpenDART and archive financial disclosure revisions by receipt ID."""
import argparse
import csv
import json
import os
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
API = "https://opendart.fss.or.kr/api"
KST = ZoneInfo("Asia/Seoul")


def fetch(session, endpoint, key, params):
    try:
        response = session.get(f"{API}/{endpoint}", params={"crtfc_key": key, **params}, timeout=30)
    except requests.RequestException as exc:
        # Requests exceptions may include the API key in a URL.
        raise RuntimeError(f"DART {endpoint} connection error ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise RuntimeError(f"DART {endpoint} HTTP {response.status_code}")
    return response


def fetch_disclosures(client, key, start, end, corp=None, *, detail=None):
    page = 1
    while True:
        params = {"bgn_de": start.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d"),
                  "last_reprt_at": "N", "pblntf_ty": "A", "page_no": page, "page_count": 100}
        if detail:
            params["pblntf_detail_ty"] = detail
        if corp:
            params["corp_code"] = corp
        data = fetch(client, "list.json", key, params).json()
        if data.get("status") == "013":
            return
        if data.get("status") != "000":
            raise RuntimeError(f"DART disclosure lookup failed (status {data.get('status')})")
        yield from data["list"]
        if page >= int(data["total_page"]):
            return
        page += 1
        time.sleep(0.15)


def save_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        partial = Path(handle.name)
    try:
        partial.write_bytes(data)
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def poll(base, key, start, end, *, download=False):
    with (Path(base) / "12_기업개황_KSIC.csv").open(encoding="utf-8-sig") as source:
        companies = {row["corp_code"] for i, row in enumerate(csv.DictReader(source)) if i}
    inbox = Path(base) / "_raw_pull" / "dart" / "receipts"
    documents = inbox.parent / "document"
    new = 0
    missing_documents = []
    with requests.Session() as client:
        entries = (entry for detail in ("A001", "A002", "A003")
                   for entry in fetch_disclosures(client, key, start, end, detail=detail))
        for entry in entries:
            if entry.get("corp_code") not in companies:
                continue
            receipt = entry["rcept_no"]
            if len(receipt) != 14 or not receipt.isascii() or not receipt.isdigit():
                raise ValueError("Invalid DART receipt ID")
            date.fromisoformat(entry["rcept_dt"])
            target = inbox / f"{receipt}.json"
            if not target.exists():
                save_atomic(target, json.dumps(entry, ensure_ascii=False, indent=2).encode())
                new += 1
            if download and not (documents / f"{receipt}.zip").exists():
                try:
                    response = fetch(client, "document.xml", key, {"rcept_no": receipt})
                    if not response.content.startswith(b"PK\x03\x04"):
                        raise RuntimeError("DART document is unavailable")
                    save_atomic(documents / f"{receipt}.zip", response.content)
                except RuntimeError:
                    missing_documents.append(receipt)
                time.sleep(0.15)
    if missing_documents:
        raise RuntimeError(f"DART receipt documents missing: {', '.join(missing_documents)}")
    return new


def historical_backfill(base, key, end):
    """Retain the per-company metadata format used by build_metadata.py."""
    with (Path(base) / "12_기업개황_KSIC.csv").open(encoding="utf-8-sig") as source:
        companies = [row["corp_code"] for i, row in enumerate(csv.DictReader(source)) if i]
    directory = Path(base) / "_raw_pull" / "dart" / "list"
    with requests.Session() as client:
        for corp in companies:
            target = directory / f"{corp}.json"
            if target.exists():
                continue
            rows = list(fetch_disclosures(client, key, date(2019, 1, 1), end, corp))
            save_atomic(target, json.dumps({"list": rows}, ensure_ascii=False).encode())
            time.sleep(0.15)


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end", type=date.fromisoformat, default=datetime.now(KST).date())
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--poll", action="store_true", help="Poll the rolling live window")
    args = parser.parse_args()
    if not 1 <= args.lookback_days <= 90:
        parser.error("Lookback must be between 1 and 90 days")
    base, key = Path(os.environ["RAWDATA_DIR"]), os.environ["OPENDART_API_KEY"]
    if args.poll:
        start = args.end - timedelta(days=args.lookback_days - 1)
        print(f"New DART receipts: {poll(base, key, start, args.end, download=args.download)}")
    else:
        historical_backfill(base, key, args.end)


if __name__ == "__main__":
    main()
