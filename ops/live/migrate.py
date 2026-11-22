"""Check the serving DB, back it up to local CSV, and (with --apply) run migrations/001_serving.sql."""
import argparse
import csv
import gzip
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_KEYS = {
    "stocks": ["stock_code"],
    "prices": ["stock_code", "bas_dd"],
    "signals": ["stock_code", "baseline_date", "eval_date"],
}

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--apply", action="store_true", help="Run the migration after the checks and backup pass")
args = parser.parse_args()
load_dotenv(ROOT / ".env")
engine = create_engine(os.environ["SUPABASE_DB_URL"], hide_parameters=True)

problems = []
with engine.connect() as c:
    for table, keys in EXPECTED_KEYS.items():
        cols = c.execute(text("""SELECT column_name, data_type FROM information_schema.columns
                                 WHERE table_schema='public' AND table_name=:t ORDER BY ordinal_position"""),
                         {"t": table}).all()
        pk = c.execute(text("""SELECT a.attname FROM pg_index i
                               JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
                               WHERE i.indrelid=CAST(:t AS regclass) AND i.indisprimary"""), {"t": f"public.{table}"}).scalars().all()
        uniques = c.execute(text("""SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=:t"""),
                            {"t": table}).scalars().all()
        rls = c.scalar(text("SELECT relrowsecurity FROM pg_class WHERE oid=CAST(:t AS regclass)"), {"t": f"public.{table}"})
        policies = c.execute(text("SELECT policyname, cmd FROM pg_policies WHERE schemaname='public' AND tablename=:t"),
                             {"t": table}).all()
        print(f"\n[{table}] columns: {[f'{n}:{t}' for n, t in cols]}")
        print(f"[{table}] primary key: {pk}  RLS on: {rls}  policies: {[tuple(p) for p in policies]}")
        print(f"[{table}] indexes: {uniques}")
        if sorted(pk) != sorted(keys) and not any(
                "UNIQUE" in u and all(k in u for k in keys) for u in uniques):
            problems.append(f"{table}: needs a primary/unique key on {keys} (found {pk})")
        if not rls:
            problems.append(f"{table}: RLS is OFF now; the migration turns it ON. Anything reading this table "
                            f"with the Supabase anon key will stop working unless a read policy exists")
    print("\nwho am I:", c.scalar(text("SELECT current_user")))

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup = ROOT / "ops" / "live" / f"db_backup_{stamp}"
backup.mkdir(parents=True)
with engine.connect() as c:
    for table in EXPECTED_KEYS:
        result = c.execution_options(stream_results=True).execute(text(f"SELECT * FROM {table}"))
        with gzip.open(backup / f"{table}.csv.gz", "wt", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(result.keys())
            n = 0
            for row in result:
                writer.writerow(row)
                n += 1
        print(f"backed up {table}: {n:,} rows")
print(f"backup folder: {backup}")

if problems:
    print("\nCHECK BEFORE APPLYING:")
    for p in problems:
        print(" -", p)
if args.apply:
    if any("needs a primary" in p for p in problems):
        raise SystemExit("Not applying: key mismatch would break publishing. Send this output to Claude.")
    sql = (ROOT / "data_pipeline" / "migrations" / "001_serving.sql").read_text(encoding="utf-8")
    with engine.connect() as c:
        c.exec_driver_sql(sql)
        c.commit()
    print("\nMigration applied.")
else:
    print("\nDry run only. Re-run with --apply to migrate.")
