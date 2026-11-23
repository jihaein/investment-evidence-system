"""Check that KRX market data can be downloaded from this machine, without publishing anything.

    python ops/live/krx_check.py                 # latest past session in the calendar
    python ops/live/krx_check.py --session 2026-11-20
"""
import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
from data_pipeline.fetch.fetch_krx import load_master, pull_day  # noqa: E402
from data_pipeline.market_data import read_calendar  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=date.fromisoformat)
    args = parser.parse_args()
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    session = args.session or max(d for d in read_calendar(os.environ["MARKET_CALENDAR_CSV"]) if d < today)
    master = load_master(os.environ["RAWDATA_DIR"])
    rows = pull_day(session, master)
    print(f"KRX {session}: {len(rows)} of {len(master)} universe stocks returned")
    if not rows:
        sys.exit("KRX returned no data (blocked, login failed, or not a trading day)")


if __name__ == "__main__":
    main()
