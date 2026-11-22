"""Refresh the serving tables from validated market data and approved PIT snapshots."""
import argparse
import csv
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from data_pipeline.compute_signals import RULE_VERSION, compute
from data_pipeline.fetch.fetch_krx import load_master, pull_day, save_market
from data_pipeline.load_prices import read_prices, records
from data_pipeline.market_data import MARKET_DIR, due_sessions, read_calendar
from data_pipeline.publication import lock_publication, publish, rollback_latest
from models.seoseohi.fscore import _snapshot_files

ROOT = Path(__file__).resolve().parents[1]
KST = ZoneInfo("Asia/Seoul")
SNAPSHOTS = Path("추가자료") / "기준일별_원본"
SNAPSHOT_FILES = (
    *[str(path.relative_to(".")) for path in _snapshot_files(Path("FScore")).values()],
    "Ohlson/04_재무_Ohlson용.csv", "DuPont/11_재무_DuPont_TTM.csv", "DuPont/11B_DuPont_날짜연결.csv",
)


def expected_codes(session, master):
    with Path(os.environ["MARKET_COVERAGE_CSV"]).open(encoding="utf-8-sig") as handle:
        expected = {row["stock_code"] for row in csv.DictReader(handle)
                    if date.fromisoformat(row["active_from"]) <= session
                    and (not row["active_until"] or session < date.fromisoformat(row["active_until"]))}
    if not expected or not expected <= master.keys():
        raise ValueError("Price coverage must define expected stock codes in the master")
    return expected


def locate_snapshot(recorded):
    """Find a published snapshot on this computer.

    refresh_runs stores the absolute path from the machine that published it. On another
    machine (or after a restore from cloud storage) the same folder name lives under
    DART_SNAPSHOT_DIR.
    """
    path = Path(recorded)
    if path.is_dir():
        return str(path)
    folder = os.getenv("DART_SNAPSHOT_DIR")
    if folder and (Path(folder) / path.name).is_dir():
        return str((Path(folder) / path.name).resolve())
    raise FileNotFoundError(
        f"Published snapshot {path.name} is not on this computer. "
        "Restore it with: python ops/cloud/sync.py pull --only dart_snapshots")


def financial_catalog(connection):
    rows = connection.execute(text("""
        SELECT financial_as_of, snapshot_path
        FROM refresh_runs WHERE reverted_at IS NULL AND snapshot_path IS NOT NULL
        ORDER BY id
    """)).all()
    return {day: locate_snapshot(path) for day, path in rows}


@contextmanager
def scoring_inputs(base, catalog):
    """Assemble a read-only input view with only the published snapshot revisions."""
    if not catalog:
        yield base
        return
    with TemporaryDirectory(prefix="seoseohi-inputs-") as folder:
        view = Path(folder)
        (view / SNAPSHOTS).mkdir(parents=True)
        (view / MARKET_DIR).symlink_to(base / MARKET_DIR, target_is_directory=True)
        master = "12_기업개황_KSIC.csv"
        (view / master).symlink_to(base / master)
        for path in (base / SNAPSHOTS).iterdir():
            if path.is_dir():
                if (path / "snapshots").is_dir():
                    raise ValueError("Convert old snapshot layout to dated model directories before live publication")
                if (path / "FScore").is_dir():
                    (view / SNAPSHOTS / path.name).symlink_to(path, target_is_directory=True)
        for day, path in catalog.items():
            target = view / SNAPSHOTS / day.isoformat()
            target.unlink(missing_ok=True)
            target.symlink_to(path, target_is_directory=True)
        yield view


def price_batch(base, session, expected, master, source=None, *, offline=False):
    target = base / MARKET_DIR / f"krx_session_{session}.csv"
    with TemporaryDirectory(prefix="krx-") as staging:
        if source is None:
            if target.exists():
                source = target
            elif offline:
                raise ValueError(f"No archived prices for {session}")
            else:
                source = Path(staging) / "session.csv"
                save_market(pull_day(session, master), source)
        with source.open(encoding="utf-8-sig") as handle:
            raw = list(csv.DictReader(handle))
        codes = [row["stock_code"] for row in raw]
        if len(codes) != len(set(codes)):
            raise ValueError(f"Duplicate stock in KRX batch for {session}")
        stocks, frame = read_prices([source])
        if (set(frame["bas_dd"]) != {session} or not expected <= set(codes)
                or not set(codes) <= master.keys()):
            raise ValueError(f"KRX batch for {session} has the wrong session or incomplete coverage")
        covered = frame[frame["stock_code"].isin(expected)]
        if (covered[["mktcap", "list_shrs", "close_prc"]].le(0).any().any()
                or covered[["mktcap", "list_shrs", "close_prc"]].isna().any().any()
                or not covered["quality_status"].eq("OK").all()):
            raise ValueError(f"KRX batch for {session} contains invalid market values")
        if source != target:
            from data_pipeline.fetch.fetch_disclosures import save_atomic

            save_atomic(target, source.read_bytes())
        return records(stocks[stocks["stock_code"].isin(expected)]), records(covered)


def refresh_prices(engine, base, sessions, *, start=None, source=None, exact=None, offline=False):
    master = load_master(base)
    with engine.connect() as connection:
        latest = connection.scalar(text("""
            SELECT max(price_session) FROM refresh_runs WHERE reverted_at IS NULL
        """))
    if exact is not None and exact not in sessions:
        raise ValueError(f"{exact} is absent from the configured market calendar")
    if exact is None and latest is None and start is None:
        raise ValueError("Specify MARKET_START_SESSION for the first automatic refresh")
    due = [exact] if exact else [day for day in sessions
                                if (start is None or day >= start) and (latest is None or day > latest)]
    for session in due:
        with engine.begin() as connection:
            lock_publication(connection)
            key = f"market:{session}:{RULE_VERSION}"
            if connection.scalar(text("""
                SELECT id FROM refresh_runs WHERE job_key = :key AND reverted_at IS NULL
            """), {"key": key}) is not None:
                print(f"Market session {session} is already published")
                continue
            stocks, prices = price_batch(base, session, expected_codes(session, master), master, source, offline=offline)
            with scoring_inputs(base, financial_catalog(connection)) as view:
                scores = compute(view, list(master), [session])
            release = publish(connection, key, prices, scores, stocks=stocks, price_session=session)
        print(f"Published market session {session} (release {release})")


def approved_snapshot(base, snapshot, sessions):
    snapshot = snapshot.resolve(strict=True)
    meta = json.loads((snapshot / "approved.json").read_text(encoding="utf-8"))
    effective = date.fromisoformat(meta["effective_session"])
    if effective not in sessions or effective > datetime.now(KST).date():
        raise ValueError("Snapshot effective session is unavailable")
    if not meta.get("approved_by") or not meta.get("receipts"):
        raise ValueError("Missing snapshot approval or receipt IDs")
    for name in SNAPSHOT_FILES:
        digest = hashlib.sha256((snapshot / name).read_bytes()).hexdigest()
        if digest != meta["sha256"].get(name):
            raise ValueError(f"Snapshot checksum mismatch: {name}")
    # Original report receipt dates are date-only. Use the next trading
    # session for full-day evaluation rather than guessing intraday availability.
    available_receipts = set()
    for name in SNAPSHOT_FILES:
        with (snapshot / name).open(encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            next(reader, None)  # Korean labels
            for row in reader:
                disclosed = row.get("disclosed_date")
                if disclosed and date.fromisoformat(disclosed) >= effective:
                    raise ValueError(f"Statement in {name} was disclosed on or after its effective session")
                available = row.get("available_from")
                if available and date.fromisoformat(available) > effective:
                    raise ValueError(f"Statement in {name} is not yet available")
                if row.get("rcept_no"):
                    available_receipts.add(row["rcept_no"])
    for receipt in meta["receipts"]:
        if len(receipt) != 14 or not receipt.isascii() or not receipt.isdigit():
            raise ValueError("Invalid receipt ID in financial approval")
        archived = base / "_raw_pull" / "dart" / "receipts" / f"{receipt}.json"
        original = json.loads(archived.read_text(encoding="utf-8"))
        filed = date.fromisoformat(original["rcept_dt"])
        if original["rcept_no"] != receipt or filed >= effective or receipt not in available_receipts:
            raise ValueError(f"Invalid effective session or missing statement for receipt {receipt}")
    return effective, meta


def refresh_financials(engine, base, snapshot, sessions, *, allow_same_session=False):
    effective, meta = approved_snapshot(base, snapshot, sessions)
    digest = hashlib.sha256(json.dumps(meta, sort_keys=True).encode()).hexdigest()
    key = f"financial:{digest}:{RULE_VERSION}"
    with engine.begin() as connection:
        lock_publication(connection)
        if connection.scalar(text("""
            SELECT id FROM refresh_runs WHERE job_key = :key AND reverted_at IS NULL
        """), {"key": key}) is not None:
            return
        catalog = financial_catalog(connection)
        if (effective in catalog and not allow_same_session) or any(day > effective for day in catalog):
            raise ValueError("Submit amended data at a new effective session after prior published financial snapshots")
        catalog[effective] = str(snapshot.resolve())
        dates = connection.scalars(text("""
            SELECT DISTINCT eval_date FROM signals WHERE eval_date >= :effective
        """), {"effective": effective}).all()
        master = load_master(base)
        with scoring_inputs(base, catalog) as view:
            scores = compute(view, list(master), sorted({effective, *dates}))
        approved_snapshot(base, snapshot, sessions)
        release = publish(connection, key, [], scores, financial_as_of=effective,
                          snapshot_path=str(snapshot.resolve()))
    print(f"Published reviewed financial snapshot {effective} (release {release})")


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="job", required=True)
    market = sub.add_parser("market")
    market.add_argument("--session", type=date.fromisoformat)
    market.add_argument("--price-csv", type=Path)
    market.add_argument("--start", type=date.fromisoformat)
    financial = sub.add_parser("financial")
    financial.add_argument("snapshot", type=Path)
    financial_inbox = sub.add_parser("financial-inbox")
    financial_inbox.add_argument("directory", type=Path)
    rollback = sub.add_parser("rollback")
    rollback.add_argument("release", type=int)
    args = parser.parse_args()

    engine = create_engine(os.environ["SUPABASE_DB_URL"])
    if args.job == "rollback":
        with engine.begin() as connection:
            rollback_latest(connection, args.release)
        print(f"Rolled back release {args.release}")
        return

    base = Path(os.environ["RAWDATA_DIR"]).resolve(strict=True)
    today = datetime.now(KST)
    trading_days = read_calendar(os.environ["MARKET_CALENDAR_CSV"])
    if not trading_days or trading_days[-1] < today.date():
        raise ValueError("Update the KRX calendar with upcoming sessions")
    if args.job == "financial":
        refresh_financials(engine, base, args.snapshot, trading_days)
    elif args.job == "financial-inbox":
        ready = []
        for manifest in args.directory.iterdir():
            if not (manifest / "approved.json").is_file():
                continue
            approval = json.loads((manifest / "approved.json").read_text(encoding="utf-8"))
            session = date.fromisoformat(approval["effective_session"])
            if session <= today.date():
                ready.append((session, manifest))
        for _, path in sorted(ready):
            refresh_financials(engine, base, path, trading_days)
    else:
        if args.price_csv and not args.session:
            parser.error("--price-csv requires --session")
        due = due_sessions(trading_days, today, os.getenv("MARKET_READY_AFTER_KST", "19:00"))
        first = os.getenv("MARKET_START_SESSION")
        refresh_prices(engine, base, due, start=args.start or (date.fromisoformat(first) if first else None),
                       source=args.price_csv, exact=args.session)


if __name__ == "__main__":
    main()
