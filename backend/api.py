"""
api.py — 서서히 신호 조회 API
실행: .venv/bin/python -m uvicorn backend.api:app --reload
"""
import os
from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv("/Users/jihaein/Developer/investment-evidence-system/.env")
engine = create_engine(os.environ["SUPABASE_DB_URL"])
app = FastAPI(title="서서히 Signal API")


@app.get("/")
def health():
    return {"status": "ok", "service": "서서히 signal api"}


@app.get("/signal")
def get_signal(stock_code: str, buy_date: str, as_of: str | None = None):
    """보유 종목의 현재 서서히 판정.
    buy_date(매수일) → 직전 스냅샷을 '기준선'으로,
    as_of(평가 기준일, 없으면 최신) → 직전 스냅샷을 '평가일'로 매핑해서 조회한다."""
    sql = text("""
        select baseline_date, eval_date,
               dupont_score, ohlson_score, fscore, sum_total,
               verdict_count, verdict_sum
        from signals
        where stock_code = :code
          and baseline_date <= :buy
          and (:as_of is null or eval_date <= :as_of)
        order by baseline_date desc, eval_date desc
        limit 1
    """)
    with engine.connect() as con:
        row = con.execute(sql, {"code": stock_code, "buy": buy_date, "as_of": as_of}).mappings().first()
    if row is None:
        raise HTTPException(404, f"{stock_code} / {buy_date} 에 대한 신호를 찾을 수 없음")
    return dict(row)


@app.get("/stock/{stock_code}")
def get_stock(stock_code: str):
    """종목 기본정보 (이름·시장·업종)."""
    with engine.connect() as con:
        row = con.execute(text("select * from stocks where stock_code = :c"),
                          {"c": stock_code}).mappings().first()
    if row is None:
        raise HTTPException(404, "해당 종목 없음")
    return dict(row)