"""Exit 3 if the latest due KRX session is not published yet (used after each scheduled run)."""
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from data_pipeline.market_data import due_sessions, read_calendar  # noqa: E402

load_dotenv(ROOT / ".env")
now = datetime.now(ZoneInfo("Asia/Seoul"))
due = due_sessions(read_calendar(os.environ["MARKET_CALENDAR_CSV"]), now, os.getenv("MARKET_READY_AFTER_KST", "19:00"))
with create_engine(os.environ["SUPABASE_DB_URL"], hide_parameters=True).connect() as connection:
    latest = connection.scalar(text("SELECT max(price_session) FROM refresh_runs WHERE reverted_at IS NULL"))
expected = due[-1] if due else None
print(f"published session {latest}, expected {expected}")
sys.exit(3 if expected and (latest is None or latest < expected) else 0)
