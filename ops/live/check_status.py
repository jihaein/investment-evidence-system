"""Read-only: show what the serving database currently holds."""
import os
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
engine = create_engine(os.environ["SUPABASE_DB_URL"], hide_parameters=True)
with engine.connect() as c:
    tables = set(inspect(c).get_table_names())
    print("tables:", sorted(tables))
    checks = {
        "prices": "SELECT max(bas_dd), count(*) FROM prices",
        "signals": "SELECT max(eval_date), count(*) FROM signals",
        "stocks": "SELECT count(*) FROM stocks",
    }
    for name, sql in checks.items():
        if name in tables:
            print(name, c.execute(text(sql)).one())
    if "refresh_runs" in tables:
        print("refresh_runs (latest 5):")
        for row in c.execute(text("SELECT id, job_key, price_session, financial_as_of, reverted_at "
                                  "FROM refresh_runs ORDER BY id DESC LIMIT 5")):
            print("  ", tuple(row))
    else:
        print("refresh_runs: MISSING (migration 001_serving.sql not applied)")
