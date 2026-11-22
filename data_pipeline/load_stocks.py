"""Import the maintained security master and approved delisting dates."""
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
if not __package__:
    sys.path.insert(0, str(ROOT))

from data_pipeline.load_prices import records
from data_pipeline.publication import json_encode, lock_publication


def read_stocks(csv_path):
    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig", skiprows=[1])
    df["stock_code"] = df["stock_code"].str.zfill(6)
    return df.rename(columns={"induty_code": "ksic_code"})[
        ["stock_code", "stock_name", "ksic_code", "corp_code"]]


def upsert_stocks(stocks, db_url):
    engine = create_engine(db_url)
    with engine.begin() as con:
        lock_publication(con)
        con.execute(text("""
            INSERT INTO stocks (stock_code, stock_name, ksic_code, corp_code, delisted_on)
            SELECT stock_code, stock_name, ksic_code, corp_code, delisted_on
            FROM jsonb_populate_recordset(NULL::stocks, CAST(:rows AS jsonb))
            ON CONFLICT (stock_code) DO UPDATE SET
                stock_name = excluded.stock_name,
                ksic_code = excluded.ksic_code,
                corp_code = excluded.corp_code,
                delisted_on = COALESCE(excluded.delisted_on, stocks.delisted_on)
        """), {"rows": json_encode(records(stocks))})
        return con.scalar(text("SELECT count(*) FROM stocks WHERE ksic_code IS NOT NULL"))


def main():
    load_dotenv(ROOT / ".env")
    stocks = read_stocks(Path(os.environ["RAWDATA_DIR"]) / "12_기업개황_KSIC.csv")
    listing_csv = os.getenv("MARKET_LISTING_EVENTS_CSV")
    if listing_csv:
        listing = pd.read_csv(listing_csv, dtype=str, keep_default_na=False)
        if listing["stock_code"].duplicated().any() or not listing["stock_code"].isin(stocks["stock_code"]).all():
            raise ValueError("Listing event file has duplicate or unknown codes")
        listing["delisted_on"] = listing["delisted_on"].map(lambda day: date.fromisoformat(day) if day else None)
        stocks = stocks.merge(listing[["stock_code", "delisted_on"]], how="left", on="stock_code")
    n = upsert_stocks(stocks, os.environ["SUPABASE_DB_URL"])
    print(f"stocks 보강 완료: ksic_code 채워진 종목: {n}개")


if __name__ == "__main__":
    main()
