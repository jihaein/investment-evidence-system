"""
서서히 신호 조회 API.
실행: .venv/bin/python -m uvicorn backend.api:app --reload
문서: http://localhost:8000/docs
"""
import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv("/Users/jihaein/Developer/investment-evidence-system/.env")
engine = create_engine(os.environ["SUPABASE_DB_URL"])
app = FastAPI(title="서서히 Signal API", version="0.2")


def _latest_signal(con, stock_code: str, buy_date: str, as_of: Optional[str] = None):
    """buy_date → 직전 스냅샷을 '기준선'으로, as_of(없으면 최신) → 직전 스냅샷을 '평가일'로
    스냅해서 가장 최근 신호 1건을 돌려준다.
    delisted: 그 평가일이 종목의 마지막 거래일보다 뒤면 True(상장폐지 이후 평가일)."""
    sql = text("""
        select g.stock_code, s.stock_name,
               g.baseline_date, g.eval_date,
               g.dupont_score, g.ohlson_score, g.fscore, g.sum_total,
               g.verdict_count, g.verdict_sum,
               (select max(bas_dd) from prices p where p.stock_code = g.stock_code) as last_traded
        from signals g
        join stocks s on s.stock_code = g.stock_code
        where g.stock_code = :code
          and g.baseline_date <= :buy
          and (:as_of is null or g.eval_date <= :as_of)
        order by g.baseline_date desc, g.eval_date desc
        limit 1
    """)
    row = con.execute(sql, {"code": stock_code, "buy": buy_date, "as_of": as_of}).mappings().first()
    if row is None:
        return None
    d = dict(row)
    last_traded = d.pop("last_traded", None)
    d["last_traded"] = last_traded
    d["delisted"] = bool(last_traded is not None and d["eval_date"] > last_traded)
    return d


@app.get("/")
def health():
    return {"status": "ok", "service": "서서히 signal api", "version": "0.2"}


@app.get("/stocks")
def list_stocks():
    """전체 종목 목록 (코드·이름·시장·업종)."""
    with engine.connect() as con:
        rows = con.execute(text(
            "select stock_code, stock_name, market, ksic_code "
            "from stocks order by stock_code"
        )).mappings().all()
    return [dict(r) for r in rows]


@app.get("/signal")
def get_signal(stock_code: str, buy_date: str, as_of: str | None = None):
    """보유 종목 1개의 현재 서서히 판정."""
    with engine.connect() as con:
        res = _latest_signal(con, stock_code, buy_date, as_of)
    if res is None:
        raise HTTPException(404, f"{stock_code} / {buy_date} 에 대한 신호를 찾을 수 없음")
    return res


@app.get("/stock/{stock_code}/history")
def signal_history(stock_code: str, baseline_date: str):
    """한 종목의 특정 매수기준선 기준, 평가일별 판정 추이(차트/그래프용)."""
    with engine.connect() as con:
        rows = con.execute(text("""
            select eval_date, dupont_score, ohlson_score, fscore, sum_total, verdict_sum
            from signals
            where stock_code = :code and baseline_date = :base
            order by eval_date
        """), {"code": stock_code, "base": baseline_date}).mappings().all()
    if not rows:
        raise HTTPException(404, "해당 (종목·기준선) 조합의 신호가 없음")
    return [dict(r) for r in rows]


class Holding(BaseModel):
    stock_code: str
    buy_date: str   # "YYYY-MM-DD"


@app.post("/portfolio")
def portfolio(holdings: list[Holding], as_of: str | None = None):
    """보유 종목 여러 개를 한 번에 평가 → 각 종목의 현재 판정 리스트.
    게임의 '내 포트폴리오' 화면에서 쓸 핵심 엔드포인트.
    요청 예시 body: [{"stock_code":"053660","buy_date":"2020-04-01"}, ...]"""
    out = []
    with engine.connect() as con:
        for h in holdings:
            res = _latest_signal(con, h.stock_code, h.buy_date, as_of)
            if res is None:
                out.append({"stock_code": h.stock_code, "buy_date": h.buy_date,
                            "error": "신호 없음"})
            else:
                out.append(res)
    return out
