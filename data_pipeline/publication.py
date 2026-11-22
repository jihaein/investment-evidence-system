"""Publish validated changes in one PostgreSQL transaction and record their undo."""
import json
from datetime import date, datetime

from sqlalchemy import text

PRICE_KEYS = ("stock_code", "bas_dd")
SIGNAL_KEYS = ("stock_code", "baseline_date", "eval_date")
PRICES = (*PRICE_KEYS, "market", "mktcap", "list_shrs", "close_prc", "quality_status")
SIGNALS = (*SIGNAL_KEYS, "dupont_score", "ohlson_score", "fscore", "sum_total",
           "verdict_count", "verdict_sum", "rule_version", "evidence", "release_id")


def json_encode(value):
    def default(obj):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        raise TypeError(type(obj).__name__)
    return json.dumps(value, default=default, allow_nan=False, ensure_ascii=False)


def lock_publication(connection):
    if not connection.scalar(text("SELECT pg_try_advisory_xact_lock(42301, hashtext(current_schema()))")):
        raise RuntimeError("Another publication is running")


def _recordset(table):
    return f"jsonb_populate_recordset(NULL::{table}, CAST(:rows AS jsonb))"


def _upsert(connection, table, columns, keys, payload):
    cols = ", ".join(columns)
    updates = ", ".join(f"{name} = excluded.{name}" for name in columns if name not in keys)
    connection.execute(text(f"""
        INSERT INTO {table} ({cols}) SELECT {cols} FROM {_recordset(table)}
        ON CONFLICT ({", ".join(keys)}) DO UPDATE SET {updates}
    """), {"rows": payload})


def _before(connection, table, keys, payload):
    match = " AND ".join(f"current.{key} = incoming.{key}" for key in keys)
    # Return JSON as text to preserve decimal market price precision through rollback.
    return connection.scalar(text(f"""
        SELECT COALESCE(jsonb_agg(to_jsonb(current)), '[]'::jsonb)::text
        FROM {table} AS current JOIN {_recordset(table)} AS incoming ON {match}
    """), {"rows": payload})


def publish(connection, job_key, prices, signals, *, stocks=(), price_session=None, financial_as_of=None, snapshot_path=None):
    lock_publication(connection)
    if job_key:
        previous = connection.scalar(text("""
            SELECT id FROM refresh_runs WHERE job_key = :key AND reverted_at IS NULL
        """), {"key": job_key})
        if previous is not None:
            return previous
    if not prices and not signals:
        raise ValueError("Cannot publish an empty update")

    if stocks:
        connection.execute(text(f"""
            INSERT INTO stocks (stock_code, stock_name, market)
            SELECT stock_code, stock_name, market FROM {_recordset("stocks")}
            ON CONFLICT (stock_code) DO NOTHING
        """), {"rows": json_encode(stocks)})

    px = json_encode(prices)
    scores = json_encode(signals)
    run_id = connection.scalar(text("""
        INSERT INTO refresh_runs
            (job_key, price_session, financial_as_of, snapshot_path, before_prices, before_signals, price_keys, signal_keys)
        VALUES (:key, :session, :financial, :snapshot, CAST(:old_prices AS jsonb), CAST(:old_signals AS jsonb),
                CAST(:price_keys AS jsonb), CAST(:signal_keys AS jsonb))
        RETURNING id
    """), {
        "key": job_key, "session": price_session, "financial": financial_as_of, "snapshot": snapshot_path,
        "old_prices": _before(connection, "prices", PRICE_KEYS, px),
        "old_signals": _before(connection, "signals", SIGNAL_KEYS, scores),
        "price_keys": json_encode([{k: row[k] for k in PRICE_KEYS} for row in prices]),
        "signal_keys": json_encode([{k: row[k] for k in SIGNAL_KEYS} for row in signals]),
    })
    _upsert(connection, "prices", PRICES, PRICE_KEYS, px)
    _upsert(connection, "signals", SIGNALS, SIGNAL_KEYS,
            json_encode([dict(row, release_id=run_id) for row in signals]))
    return run_id


def rollback_latest(connection, run_id):
    lock_publication(connection)
    row = connection.execute(text("""
        SELECT id, before_prices::text, before_signals::text, price_keys::text, signal_keys::text
        FROM refresh_runs WHERE reverted_at IS NULL ORDER BY id DESC LIMIT 1
    """)).one_or_none()
    if row is None or row.id != run_id:
        raise ValueError("Only the latest active release can be rolled back")
    for table, columns, keys, old_rows, new_keys in (
        ("prices", PRICES, PRICE_KEYS, row.before_prices, row.price_keys),
        ("signals", SIGNALS, SIGNAL_KEYS, row.before_signals, row.signal_keys),
    ):
        match = " AND ".join(f"target.{key} = incoming.{key}" for key in keys)
        connection.execute(text(f"""
            DELETE FROM {table} AS target USING {_recordset(table)} AS incoming WHERE {match}
        """), {"rows": new_keys})
        _upsert(connection, table, columns, keys, old_rows)
    connection.execute(text("UPDATE refresh_runs SET reverted_at = now() WHERE id = :id"), {"id": run_id})
