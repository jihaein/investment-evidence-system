"""The historical price files and per-session KRX files in publication order."""
import csv
from datetime import date, time
from pathlib import Path

MARKET_DIR = Path("추가자료") / "시장자료"


def market_files(base_dir):
    paths = sorted((Path(base_dir) / MARKET_DIR).glob("*.csv"))
    # The legacy KRX file supersedes the historical export. An archived
    # per-session batch takes precedence over both.
    return sorted(paths, key=lambda p: (p.name.startswith("krx_"), p.name.startswith("krx_session_"), p.name))



def read_calendar(path):
    with Path(path).open(encoding="utf-8-sig") as handle:
        return sorted({date.fromisoformat(row["session"]) for row in csv.DictReader(handle)})


def due_sessions(days, now, ready_after):
    cutoff = time.fromisoformat(ready_after)
    return [day for day in days if day < now.date() or (day == now.date() and now.time() >= cutoff)]
