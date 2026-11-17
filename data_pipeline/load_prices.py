"""
load_prices.py — 일별 시세 CSV를 Supabase(Postgres)의 stocks/prices 테이블에 적재.
실행:  .venv/bin/python data_pipeline/load_prices.py
"""
import os
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# hard coded here
load_dotenv("/Users/jihaein/Developer/investment-evidence-system/.env")
DB_URL = os.environ["SUPABASE_DB_URL"]
CSV_PATH = (
    "/Users/jihaein/Library/CloudStorage/GoogleDrive-j.in26mcgill@gmail.com/"
    ".shortcut-targets-by-id/1RS50TgL31WxhExC3ZVXca6bMTj-c9yCU/skku_fds_main/"
    "데이터/서서히 모델 백테스트/개발자전달_본데이터_120종목_백테스트_2020-2026/"
    "추가자료/시장자료/120종목_일별시가총액_2019-12-30_2026-11-11.csv"
)


def read_prices(csv_path):
    """CSV를 읽어 (stocks, prices) 두 DataFrame으로 정리한다."""
    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig")
    df["stock_code"]  = df["stock_code"].str.zfill(6)
    df["bas_dd"]      = pd.to_datetime(df["BAS_DD"], format="%Y%m%d").dt.date
    df["MKTCAP"]      = pd.to_numeric(df["MKTCAP"], errors="coerce").astype("Int64")
    df["LIST_SHRS"]   = pd.to_numeric(df["LIST_SHRS"], errors="coerce").astype("Int64")
    df["TDD_CLSPRC"]  = pd.to_numeric(df["TDD_CLSPRC"], errors="coerce")

    stocks = (df[["stock_code", "stock_name_2014", "MKT_NM"]]
              .drop_duplicates("stock_code")
              .rename(columns={"stock_name_2014": "stock_name", "MKT_NM": "market"}))
    prices = (df.rename(columns={"MKT_NM": "market", "MKTCAP": "mktcap",
                                 "LIST_SHRS": "list_shrs", "TDD_CLSPRC": "close_prc"})
                [["stock_code", "bas_dd", "market", "mktcap", "list_shrs",
                  "close_prc", "quality_status"]])
    return stocks, prices


def load(stocks, prices, db_url):
    """테이블을 비우고 stocks → prices 순서로 다시 적재한다."""
    engine = create_engine(db_url)
    with engine.begin() as con:
        con.execute(text("truncate table prices, stocks, signals cascade"))
    stocks.to_sql("stocks", engine, if_exists="append", index=False)
    prices.to_sql("prices", engine, if_exists="append", index=False,
                  method="multi", chunksize=1000)
    with engine.connect() as con:
        ns  = con.execute(text("select count(*) from stocks")).scalar()
        npx = con.execute(text("select count(*) from prices")).scalar()
    return ns, npx


def main():
    stocks, prices = read_prices(CSV_PATH)
    ns, npx = load(stocks, prices, DB_URL)
    print(f"✓ 적재 완료 — stocks: {ns}종목 | prices: {npx:,}행")


if __name__ == "__main__":
    main()