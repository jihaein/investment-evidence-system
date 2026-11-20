"""Read market CSVs and publish prices to PostgreSQL."""
import os
import sys
from pathlib import Path
from uuid import uuid4

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
if not __package__:
    sys.path.insert(0, str(ROOT))

from data_pipeline.market_data import market_files


def read_prices(csv_paths):
    """Return the latest stocks and one price row per stock and session."""
    if not csv_paths:
        raise ValueError("No market CSVs")
    df = pd.concat([pd.read_csv(path, dtype=str, encoding="utf-8-sig") for path in csv_paths], ignore_index=True)
    if df.empty or not df["stock_code"].str.fullmatch(r"[0-9]{1,6}").fillna(False).all():
        raise ValueError("Missing or invalid stock codes in market data")
    df["stock_code"] = df["stock_code"].str.zfill(6)
    df["bas_dd"] = pd.to_datetime(df["BAS_DD"], format="%Y%m%d").dt.date
    if df["bas_dd"].isna().any():
        raise ValueError("Missing market session")
    df["MKTCAP"] = pd.to_numeric(df["MKTCAP"], errors="coerce").astype("Int64")
    df["LIST_SHRS"] = pd.to_numeric(df["LIST_SHRS"], errors="coerce").astype("Int64")
    df["TDD_CLSPRC"] = pd.to_numeric(df["TDD_CLSPRC"], errors="coerce")
    stocks = (df.sort_values("bas_dd", kind="stable")[["stock_code", "stock_name_2014", "MKT_NM"]]
              .drop_duplicates("stock_code", keep="last")
              .rename(columns={"stock_name_2014": "stock_name", "MKT_NM": "market"}))
    prices = (df.rename(columns={"MKT_NM": "market", "MKTCAP": "mktcap",
                                 "LIST_SHRS": "list_shrs", "TDD_CLSPRC": "close_prc"})
                [["stock_code", "bas_dd", "market", "mktcap", "list_shrs", "close_prc", "quality_status"]]
                  .drop_duplicates(["stock_code", "bas_dd"], keep="last"))
    return stocks, prices


def records(frame):
    return frame.astype(object).where(pd.notna(frame), None).to_dict("records")


def load(stocks, prices, db_url):
    from data_pipeline.publication import publish

    engine = create_engine(db_url)
    with engine.begin() as connection:
        publish(connection, f"price-import:{uuid4()}", records(prices), [], stocks=records(stocks))
        return tuple(connection.execute(text(
            "SELECT (SELECT count(*) FROM stocks), (SELECT count(*) FROM prices)"
        )).one())


def main():
    load_dotenv(ROOT / ".env")
    paths = market_files(os.environ["RAWDATA_DIR"])
    stocks, prices = read_prices(paths)
    ns, npx = load(stocks, prices, os.environ["SUPABASE_DB_URL"])
    print(f"Loaded {ns:,} stocks and {npx:,} prices from {len(paths)} files")


if __name__ == "__main__":
    main()
