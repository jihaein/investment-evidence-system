"""Fetch KRX end-of-day data for the configured stock universe."""
import argparse
import os
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
MASTER = "12_기업개황_KSIC.csv"
MARKET_DIR = Path("추가자료") / "시장자료"
CLS_TO_MKT = {"K": "코스닥", "Y": "유가증권", "E": "코넥스"}
COLUMNS = ["stock_code", "stock_name_2014", "MKT_NM", "BAS_DD", "MKTCAP",
           "LIST_SHRS", "TDD_CLSPRC", "quality_status"]


def load_master(base):
    df = pd.read_csv(Path(base) / MASTER, dtype=str, skiprows=[1], encoding="utf-8-sig")
    return {r.stock_code: (r.stock_name, CLS_TO_MKT.get(r.corp_cls, r.corp_cls))
            for r in df.itertuples(index=False)}


def pull_day(session, master):
    # KRX credentials must exist before the first import of pykrx.
    from pykrx import stock

    tag = session.strftime("%Y%m%d")
    df = stock.get_market_cap_by_ticker(tag, market="ALL")
    rows = []
    for code, row in df.iterrows():
        if code in master:
            name, market = master[code]
            rows.append(dict(zip(COLUMNS, (code, name, market, tag, row["시가총액"],
                                            row["상장주식수"], row["종가"], "OK"))))
    return rows


def save_market(rows, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
        partial = Path(handle.name)
    try:
        pd.DataFrame(rows, columns=COLUMNS).to_csv(partial, index=False, encoding="utf-8-sig")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=lambda value: date.fromisoformat(value))
    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="end")
    args = parser.parse_args()
    if bool(args.start) != bool(args.end) or (args.date and args.start):
        parser.error("Pass --date or both --from and --to")
    from pykrx import stock

    base = Path(os.environ["RAWDATA_DIR"])
    master = load_master(base)
    if args.start:
        dates = pd.bdate_range(args.start, args.end).date
    else:
        dates = [args.date or date.fromisoformat(stock.get_nearest_business_day_in_a_week())]
    found = 0
    for day in dates:
        rows = pull_day(day, master)
        if rows:
            save_market(rows, base / MARKET_DIR / f"krx_session_{day}.csv")
            found += 1
    if not found:
        raise SystemExit("No market data returned. Check the trading calendar and provider.")
    print(f"Archived {found} session(s)")


if __name__ == "__main__":
    main()
