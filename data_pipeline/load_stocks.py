"""
load_stocks.py — 기업개황(KSIC) CSV로 stocks 테이블을 보강(upsert).
prices 가 stocks 를 FK로 참조하므로, truncate 대신 upsert('있으면 갱신, 없으면 추가').
실행:  .venv/bin/python data_pipeline/load_stocks.py
"""
import os
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv("/Users/jihaein/Developer/investment-evidence-system/.env")
DB_URL = os.environ["SUPABASE_DB_URL"]
KSIC_CSV = (
    "/Users/jihaein/Library/CloudStorage/GoogleDrive-j.in26mcgill@gmail.com/"
    ".shortcut-targets-by-id/1RS50TgL31WxhExC3ZVXca6bMTj-c9yCU/skku_fds_main/"
    "데이터/서서히 모델 백테스트/개발자전달_본데이터_120종목_백테스트_2020-2026/"
    "12_기업개황_KSIC.csv"
)


def read_stocks(csv_path):
    # 2번째 줄은 한글 설명행 → skiprows=[1] 로 건너뛴다
    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig", skiprows=[1])
    df["stock_code"] = df["stock_code"].str.zfill(6)
    return df.rename(columns={"induty_code": "ksic_code"})[
        ["stock_code", "stock_name", "ksic_code", "corp_code"]]


def upsert_stocks(stocks, db_url):
    engine = create_engine(db_url)
    with engine.begin() as con:
        # DART 고유번호 보관용 컬럼(최초 1회만 추가됨)
        con.execute(text("alter table stocks add column if not exists corp_code text"))
        # 1) 스테이징 테이블로 통째 업로드
        stocks.to_sql("_stage_stocks", con, if_exists="replace", index=False)
        # 2) 본 테이블로 upsert — 있으면 UPDATE, 없으면 INSERT
        con.execute(text("""
            insert into stocks (stock_code, stock_name, ksic_code, corp_code)
            select stock_code, stock_name, ksic_code, corp_code from _stage_stocks
            on conflict (stock_code) do update set
                stock_name = excluded.stock_name,
                ksic_code  = excluded.ksic_code,
                corp_code  = excluded.corp_code
        """))
        con.execute(text("drop table _stage_stocks"))
    with engine.connect() as con:
        n = con.execute(text(
            "select count(*) from stocks where ksic_code is not null")).scalar()
    return n


def main():
    stocks = read_stocks(KSIC_CSV)
    n = upsert_stocks(stocks, DB_URL)
    print(f"✓ stocks 보강 완료 — ksic_code 채워진 종목: {n}개")


if __name__ == "__main__":
    main()