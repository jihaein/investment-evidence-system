"""Run the KRX and OpenDART scheduled jobs using an isolated serving database or production inputs."""
import argparse
import os
import time
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from data_pipeline.dart_conversion import json_file, new_snapshot, store_json
from data_pipeline.fetch.fetch_disclosures import poll
from data_pipeline.live_refresh import (
    ROOT, SNAPSHOTS, financial_catalog, refresh_financials, refresh_prices,
)
from data_pipeline.market_data import due_sessions, read_calendar

KST = ZoneInfo("Asia/Seoul")


def convert_receipts(engine, base, out, sessions, as_of, start, *, key=None):
    # A file lock covers the read/build/publish sequence for automatic workers
    # sharing this archive. Publication also has its own PostgreSQL lock.
    import fcntl

    out.mkdir(parents=True, exist_ok=True)
    with (out / ".converter.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with engine.connect() as connection:
            catalog = financial_catalog(connection)
        dates = {date.fromisoformat(path.name): path for path in (base / SNAPSHOTS).iterdir()
                 if (path / "FScore").is_dir()}
        dates.update({day: Path(path) for day, path in catalog.items()})
        if not dates:
            raise ValueError("Seed the worker with a dated point-in-time snapshot")

        grouped = defaultdict(list)
        inbox = base / "_raw_pull/dart/receipts"
        for path in sorted(inbox.glob("*.json")):
            receipt = json_file(path)
            filed = date.fromisoformat(receipt["rcept_dt"])
            if not start <= filed <= as_of:
                continue
            position = bisect_right(sessions, filed)
            if position == len(sessions) or sessions[position] > as_of:
                continue
            # A late receipt is admitted at the latest published financial
            # cutoff if an earlier cutoff is already closed.
            effective = max(sessions[position], max(catalog, default=date.min))
            grouped[effective].append(receipt)

        processed = set()
        for folder in catalog.values():
            manifest = json_file(Path(folder) / "approved.json")
            processed.update(manifest.get("observed_receipts", manifest["receipts"]))

        pending = {}
        for effective, entries in sorted(grouped.items()):
            entries = [entry for entry in entries if entry["rcept_no"] not in processed]
            if not entries:
                continue
            seed = dates[max(day for day in dates if day <= effective)]
            destination, errors = new_snapshot(base, seed, out, entries, effective, sessions, key=key)
            pending.update(errors)
            if destination:
                refresh_financials(engine, base, destination, sessions, allow_same_session=True)
                dates[effective] = destination
        store_json(out / "pending.json", pending)
        if pending:
            raise RuntimeError(f"{len(pending)} DART receipt(s) need retry or review; see {out / 'pending.json'}")


def poll_dart(base, out, key, start, end):
    cursor = out / "poll_cursor.json"
    if cursor.is_file():
        last = date.fromisoformat(json_file(cursor)["through"])
        start = max(start, last - timedelta(days=6))
    while start <= end:
        through = min(end, start + timedelta(days=84))
        poll(base, key, start, through, download=True)
        store_json(cursor, {"through": through.isoformat()})
        start = through + timedelta(days=1)


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--staging", action="store_true", help="Require a separate STAGING_DB_URL")
    parser.add_argument("--offline", action="store_true", help="Use archived provider inputs only")
    parser.add_argument("--as-of", type=date.fromisoformat, help="Replay cutoff; requires --offline --once")
    parser.add_argument("--jobs", choices=("all", "market", "dart"), default="all")
    args = parser.parse_args()
    if args.as_of and not (args.offline and args.once):
        parser.error("--as-of requires --offline --once")
    url = os.environ["STAGING_DB_URL" if args.staging else "SUPABASE_DB_URL"]
    engine = create_engine(url, pool_pre_ping=True, hide_parameters=True)
    base = Path(os.environ["RAWDATA_DIR"]).resolve(strict=True)
    out = Path(os.environ.get("DART_SNAPSHOT_DIR") or base.parent / "dart_snapshots").resolve()
    start = os.getenv("MARKET_START_SESSION")
    if args.jobs in {"all", "dart"}:
        dart_start = date.fromisoformat(os.environ["DART_START_DATE"])
        key = None if args.offline else os.environ["OPENDART_API_KEY"]
    market_every = float(os.getenv("MARKET_POLL_MINUTES", "15")) * 60
    dart_every = float(os.getenv("DART_POLL_MINUTES", "60")) * 60
    if market_every <= 0 or dart_every <= 0:
        parser.error("Job intervals must be positive")
    next_market = next_dart = time.monotonic()
    while True:
        now = datetime.now(KST)
        as_of = args.as_of or now.date()
        tick = time.monotonic()
        sessions = read_calendar(os.environ["MARKET_CALENDAR_CSV"])
        if not sessions or sessions[-1] < as_of:
            raise ValueError("Update the exchange calendar with upcoming sessions")
        failed = False
        if args.jobs in {"all", "dart"} and tick >= next_dart:
            next_dart = tick + dart_every
            poll_error = None
            try:
                if not args.offline:
                    # Convert receipts already captured even if part of the poll failed.
                    try:
                        poll_dart(base, out, key, dart_start, as_of)
                    except (RuntimeError, ValueError) as exc:
                        poll_error = exc
                convert_receipts(engine, base, out, sessions, as_of, dart_start, key=key)
                if poll_error:
                    raise poll_error
                print(f"{now.isoformat()} DART refresh complete", flush=True)
            except (RuntimeError, ValueError, OSError, SQLAlchemyError) as exc:
                print(f"{now.isoformat()} DART refresh failed: {exc}", flush=True)
                failed = True
        if args.jobs in {"all", "market"} and tick >= next_market:
            next_market = tick + market_every
            try:
                due = ([day for day in sessions if day <= as_of] if args.as_of else
                       due_sessions(sessions, now, os.getenv("MARKET_READY_AFTER_KST", "19:00")))
                refresh_prices(engine, base, due, start=date.fromisoformat(start) if start else None,
                               offline=args.offline)
                print(f"{now.isoformat()} KRX refresh complete", flush=True)
            except (RuntimeError, ValueError, OSError, SQLAlchemyError) as exc:
                print(f"{now.isoformat()} KRX refresh failed: {exc}", flush=True)
                failed = True
        if args.once:
            raise SystemExit(1 if failed else 0)
        until = min(next_market if args.jobs in {"all", "market"} else float("inf"),
                    next_dart if args.jobs in {"all", "dart"} else float("inf"))
        time.sleep(max(0.1, until - time.monotonic()))


if __name__ == "__main__":
    main()
