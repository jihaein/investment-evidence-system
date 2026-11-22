"""Read-only: confirm the latest signals use fresh prices and the newest filings."""
import os
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
engine = create_engine(os.environ["SUPABASE_DB_URL"], hide_parameters=True)
with engine.connect() as c:
    latest = c.scalar(text("SELECT max(eval_date) FROM signals"))
    print("latest eval_date:", latest)
    print("latest price session:", c.scalar(text("SELECT max(price_session) FROM refresh_runs WHERE reverted_at IS NULL")))
    print("latest financial snapshot:", c.scalar(text("SELECT max(financial_as_of) FROM refresh_runs WHERE reverted_at IS NULL")))
    rows = c.execute(text("""
        SELECT stock_code, baseline_date, verdict_sum, evidence
        FROM signals WHERE eval_date = :d
          AND baseline_date = (SELECT max(baseline_date) FROM signals WHERE eval_date = :d AND baseline_date < '2026-09-01')
    """), {"d": latest}).all()
    print(f"signals on {latest} (baseline {rows[0][1] if rows else None}): {len(rows)} stocks")
    print("verdicts:", dict(Counter(r[2] for r in rows)))
    receipts = Counter()
    q3 = 0
    for r in rows:
        detail = ((r[3] or {}).get("ohlson") or {}).get("detail") or {}
        dp = ((r[3] or {}).get("dupont") or {}).get("detail") or {}
        rc = dp.get("rcept_no_eval") or detail.get("rcept_no_eval") or ""
        receipts[rc[:6]] += 1
        q3 += rc >= "20261101"
    print("filing month used at evaluation (DuPont/Ohlson receipt):", dict(sorted(receipts.items())))
    print(f"stocks evaluated with a Nov-2026 (Q3) filing: {q3}/{len(rows)}")
    sample = c.execute(text("""SELECT stock_code, dupont_score, ohlson_score, fscore, verdict_sum,
                                      evidence->'dupont'->'detail'->>'rcept_no_eval' AS dp_receipt,
                                      evidence->'ohlson'->'detail'->>'market_date_eval' AS price_date
                               FROM signals WHERE eval_date = :d AND stock_code IN ('319660','095270','217730')
                               ORDER BY baseline_date DESC, stock_code LIMIT 3"""), {"d": latest}).all()
    for s in sample:
        print("  ", tuple(s))
