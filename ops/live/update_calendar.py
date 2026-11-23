"""Keep ops/live/krx_sessions.csv at least a year ahead.

Past sessions already in the file are kept as they are. Future sessions are weekdays that are not
Korea Exchange holidays (python-holidays XKRX calendar, which matched every actual trading day
2020-2026) and not listed in ops/live/krx_closures.csv (add one-off closures there, e.g. an
unscheduled holiday announced by KRX). Runs at the start of every daily refresh.

    python ops/live/update_calendar.py
"""
import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import holidays

HERE = Path(__file__).resolve().parent
SESSIONS = HERE / "krx_sessions.csv"
CLOSURES = HERE / "krx_closures.csv"


def main():
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    with SESSIONS.open(encoding="utf-8-sig") as handle:
        existing = {date.fromisoformat(row["session"]) for row in csv.DictReader(handle)}
    closures = set()
    if CLOSURES.exists():
        with CLOSURES.open(encoding="utf-8-sig") as handle:
            closures = {date.fromisoformat(row["date"]) for row in csv.DictReader(handle) if row.get("date")}
    end = date(today.year + 1, 12, 31)
    closed = holidays.financial_holidays("XKRX", years=range(today.year, end.year + 1))
    future = set()
    day = today
    while day <= end:
        if day.weekday() < 5 and day not in closed and day not in closures:
            future.add(day)
        day += timedelta(days=1)
    sessions = sorted({d for d in existing if d < today} | future)
    tmp = SESSIONS.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["session"])
        writer.writerows([[d.isoformat()] for d in sessions])
    tmp.replace(SESSIONS)
    print(f"Trading calendar: {len(sessions):,} sessions, through {sessions[-1]}")


if __name__ == "__main__":
    main()
