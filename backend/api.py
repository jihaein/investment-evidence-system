"""Read-only API for the published 서서히 signals."""
import json
import os
import secrets
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from data_pipeline.market_data import due_sessions, read_calendar

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
app = FastAPI(title="서서히 Signal API", version="0.3")
StockCode = Annotated[str, StringConstraints(pattern=r"^[0-9]{6}$")]
KST = ZoneInfo("Asia/Seoul")


@lru_cache(maxsize=1)
def get_engine():
    url = os.getenv("API_DB_URL") or os.environ["SUPABASE_DB_URL"]
    return create_engine(url, pool_pre_ping=True)


def authorize(authorization: Annotated[str | None, Header()] = None):
    token = os.getenv("API_READ_TOKEN")
    if token and not secrets.compare_digest((authorization or "").encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})


def expected_session(as_of):
    source = os.getenv("MARKET_CALENDAR_CSV")
    if not source:
        return None
    now = datetime.now(KST)
    try:
        days = read_calendar(source)
    except (OSError, ValueError, KeyError):
        raise HTTPException(503, "Market calendar could not be read") from None
    if not days or days[-1] < now.date():
        raise HTTPException(503, "Market calendar needs an update")
    eligible = due_sessions(days, now, os.getenv("MARKET_READY_AFTER_KST", "19:00"))
    return max((day for day in eligible if day <= as_of), default=None)


class Holding(BaseModel):
    stock_code: StockCode
    buy_date: date


def read_signals(connection, holdings, as_of):
    """Read each holding in one query with a shared database snapshot."""
    if not holdings:
        return []
    requested = as_of or datetime.now(KST).date()
    if any(item.buy_date > requested for item in holdings):
        raise HTTPException(422, "as_of must be on or after each buy_date")
    expected = expected_session(requested)
    items = [item.model_dump(mode="json") for item in holdings]
    rows = connection.execute(text("""
        WITH requested AS (
            SELECT value->>'stock_code' AS stock_code,
                   CAST(value->>'buy_date' AS date) AS buy_date, ord
            FROM jsonb_array_elements(CAST(:holdings AS jsonb)) WITH ORDINALITY AS input(value, ord)
        ), market AS (
            SELECT max(price_session) AS market_session
            FROM refresh_runs WHERE reverted_at IS NULL AND price_session <= :as_of
        )
        SELECT r.stock_code, r.buy_date, s.stock_name, s.delisted_on,
               g.baseline_date, g.eval_date, g.dupont_score, g.ohlson_score,
               g.fscore, g.sum_total, g.verdict_count, g.verdict_sum,
               g.rule_version, g.evidence, g.release_id,
               p.last_traded, m.market_session
        FROM requested r
        CROSS JOIN market m
        LEFT JOIN stocks s ON s.stock_code = r.stock_code
        LEFT JOIN LATERAL (
            SELECT g.* FROM signals g
            WHERE g.stock_code = r.stock_code AND g.baseline_date <= r.buy_date
              AND g.eval_date <= :as_of
            ORDER BY g.baseline_date DESC, g.eval_date DESC LIMIT 1
        ) g ON true
        LEFT JOIN LATERAL (
            SELECT max(bas_dd) AS last_traded FROM prices
            WHERE stock_code = r.stock_code AND bas_dd <= g.eval_date
              AND close_prc > 0
        ) p ON true
        ORDER BY r.ord
    """), {"holdings": json.dumps(items), "as_of": requested}).mappings()
    result = []
    for row in rows:
        if row["eval_date"] is None:
            result.append({"stock_code": row["stock_code"], "buy_date": row["buy_date"],
                           "error": "신호 없음"})
            continue
        entry = dict(row)
        delisted = bool(entry["delisted_on"] and entry["delisted_on"] <= requested)
        market_session = entry.pop("market_session")
        evidence = entry["evidence"] or {}
        ohlson = (evidence.get("ohlson") or {}).get("detail") or {}
        expected_market = expected or market_session
        stale = bool(expected_market and (entry["eval_date"] < expected_market or
                     (expected is not None and (market_session is None or market_session < expected))))
        entry.update(
            as_of=requested, delisted=delisted,
            listing_status="delisted" if delisted else ("pre_delisting" if entry["delisted_on"] else "unknown"),
            price_as_of=entry["last_traded"], market_session=market_session,
            expected_market_session=expected,
            ohlson_price_as_of=ohlson.get("market_date_eval"),
            financial_as_of=evidence.get("financial_as_of"),
            status="stale" if stale else ("held" if entry["verdict_sum"] == "유보" else "available"),
            verdict=entry["verdict_sum"],
        )
        result.append(entry)
    return result


@app.get("/")
def health():
    return {"status": "ok", "service": "서서히 signal api", "version": "0.3"}


@app.get("/ready", dependencies=[Depends(authorize)])
def readiness():
    try:
        with get_engine().connect() as con:
            state = con.execute(text("""
                SELECT (SELECT max(price_session) FROM refresh_runs WHERE reverted_at IS NULL) AS market_session,
                       (SELECT max(eval_date) FROM signals) AS evaluation_date,
                       (SELECT max(financial_as_of) FROM refresh_runs WHERE reverted_at IS NULL) AS financial_session
            """)).mappings().one()
    except (KeyError, SQLAlchemyError):
        raise HTTPException(503, "Serving database unavailable") from None
    if state["evaluation_date"] is None:
        raise HTTPException(503, "No published signals")
    expected = expected_session(datetime.now(KST).date())
    if expected and (state["market_session"] is None or state["market_session"] < expected):
        raise HTTPException(503, {
            "status": "stale",
            "market_session": str(state["market_session"]) if state["market_session"] else None,
            "expected_market_session": str(expected),
        })
    return {"status": "ok", **dict(state), "expected_market_session": expected}


@app.get("/stocks", dependencies=[Depends(authorize)])
def list_stocks():
    with get_engine().connect() as con:
        rows = con.execute(text("""
            SELECT stock_code, stock_name, market, ksic_code, delisted_on
            FROM stocks ORDER BY stock_code
        """)).mappings().all()
    return [dict(r) for r in rows]


@app.get("/signal", dependencies=[Depends(authorize)])
def get_signal(stock_code: StockCode, buy_date: date, as_of: date | None = None):
    with get_engine().connect() as con:
        row, = read_signals(con, [Holding(stock_code=stock_code, buy_date=buy_date)], as_of)
    if "error" in row:
        raise HTTPException(404, f"{stock_code} / {buy_date} 에 대한 신호를 찾을 수 없음")
    return row


@app.get("/stock/{stock_code}/history", dependencies=[Depends(authorize)])
def signal_history(stock_code: StockCode, baseline_date: date,
                   limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
                   after: date | None = None):
    with get_engine().connect() as con:
        rows = con.execute(text("""
            SELECT eval_date, dupont_score, ohlson_score, fscore, sum_total, verdict_sum,
                   rule_version, release_id
            FROM signals WHERE stock_code = :code AND baseline_date = :base
              AND eval_date > :after
            ORDER BY eval_date LIMIT :limit
        """), {"code": stock_code, "base": baseline_date, "after": after or date.min,
               "limit": limit}).mappings().all()
    if not rows and after is None:
        raise HTTPException(404, "해당 (종목·기준선) 조합의 신호가 없음")
    return [dict(r) for r in rows]


@app.post("/portfolio", dependencies=[Depends(authorize)])
def portfolio(holdings: Annotated[list[Holding], Field(max_length=200)], as_of: date | None = None):
    with get_engine().connect() as con:
        return read_signals(con, holdings, as_of)
