"""
load_prices.py — 일별 시세 CSV를 Supabase(Postgres)의 stocks/prices 테이블에 upsert.
실행:  .venv/bin/python data_pipeline/load_prices.py

원본 데이터 위치는 .env 의 RAWDATA_DIR 로 지정한다(하드코딩된 구글드라이브 경로 제거).
RAWDATA_DIR = Supabase Storage 아카이브를 풀어둔 로컬 폴더.
  예) RAWDATA_DIR=/Users/<you>/.../data/rawdata

upsert = '있으면 갱신 / 없으면 추가'. truncate 안 함 →
  · signals 안 건드림,  · stocks 는 DO NOTHING(KSIC 보강 보존),  · prices 만 DO UPDATE.
"""
import os
import glob
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv("/Users/jihaein/Developer/investment-evidence-system/.env")
DB_URL = os.environ["SUPABASE_DB_URL"]
RAWDATA_DIR = os.environ["RAWDATA_DIR"]          # 로컬 원본 폴더 (구글드라이브 X)

# 시장자료 폴더의 가격 CSV 를 파일명 자동 탐색(이름이 바뀌어도 동작)
_market = sorted(glob.glob(os.path.join(RAWDATA_DIR, "추가자료", "시장자료", "*.csv")))
if not _market:
    raise FileNotFoundError(f"가격 CSV 없음: {os.path.join(RAWDATA_DIR, '추가자료', '시장자료')}")


def read_prices(csv_paths):
    """CSV를 읽어 (stocks, prices) 두 DataFrame으로 정리한다."""
    df = pd.concat([pd.read_csv(f, dtype=str, encoding="utf-8-sig") for f in csv_paths], ignore_index=True)
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
                  "close_prc", "quality_status"]]
                  .drop_duplicates("stock_code", "bas_dd", keep="last"))
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
    stocks, prices = read_prices(_market)
    ns, npx = load(stocks, prices, DB_URL)
    print(f"✓ 적재(upsert) 완료 — stocks: {ns}종목 | prices: {npx:,}행")
    print(f"  (원본: {len(_market)}개 파일 - {', '.join(os.path.basename(f) for f in _market)})")


if __name__ == "__main__":
    main()
