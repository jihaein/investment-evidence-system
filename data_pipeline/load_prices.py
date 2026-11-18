"""
load_prices.py — 일별 시세 CSV를 Supabase(Postgres)의 stocks/prices 테이블에 upsert.
실행:  .venv/bin/python data_pipeline/load_prices.py

upsert = '있으면 갱신 / 없으면 추가'. truncate 를 안 하므로:
  · signals 테이블을 건드리지 않는다(예전엔 같이 지웠음).
  · stocks 는 DO NOTHING → load_stocks 가 넣은 KSIC·DART 보강을 덮어쓰지 않는다.
  · prices 만 DO UPDATE → 새 날짜 추가 + 수정분 반영.
"""
import os
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

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
    """upsert 적재 (스테이징 테이블 → INSERT ... ON CONFLICT)."""
    engine = create_engine(db_url)
    with engine.begin() as con:
        # stocks: 없는 종목만 기본정보 추가, 기존 행(KSIC·DART 보강)은 보존
        stocks.to_sql("_stg_stocks", con, if_exists="replace", index=False)
        con.execute(text("""
            insert into stocks (stock_code, stock_name, market)
            select stock_code, stock_name, market from _stg_stocks
            on conflict (stock_code) do nothing
        """))
        con.execute(text("drop table _stg_stocks"))
        # prices: (종목,날짜) 같으면 갱신, 없으면 추가
        prices.to_sql("_stg_prices", con, if_exists="replace", index=False,
                      method="multi", chunksize=1000)
        con.execute(text("""
            insert into prices (stock_code, bas_dd, market, mktcap, list_shrs, close_prc, quality_status)
            select stock_code, bas_dd, market, mktcap, list_shrs, close_prc, quality_status from _stg_prices
            on conflict (stock_code, bas_dd) do update set
                market=excluded.market, mktcap=excluded.mktcap, list_shrs=excluded.list_shrs,
                close_prc=excluded.close_prc, quality_status=excluded.quality_status
        """))
        con.execute(text("drop table _stg_prices"))
    with engine.connect() as con:
        ns  = con.execute(text("select count(*) from stocks")).scalar()
        npx = con.execute(text("select count(*) from prices")).scalar()
    return ns, npx


def main():
    stocks, prices = read_prices(CSV_PATH)
    ns, npx = load(stocks, prices, DB_URL)
    print(f"✓ 적재(upsert) 완료 — stocks: {ns}종목 | prices: {npx:,}행")


if __name__ == "__main__":
    main()
