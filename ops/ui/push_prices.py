"""Real KRX daily bars → the UI's Supabase (ReThinking_UI db/migrations/007: price_daily, stock_names).

Replaces the UI repo's tools/prices/push_prices.py + the teammate PC's daily KRX download. Same tables and rules:
  stock_names  every KOSPI/KOSDAQ stock on the latest uploaded day (code, name, market, close, market value)
  price_daily  daily bars (raw, unadjusted, as KRX shows them) for the stocks the app needs: the top 300 by market
               value, the 12 journal stocks and every stock in a player's garden (garden_saves)
Each run uploads the sessions after the last uploaded day (whole market fetched once per session, filtered), and
a stock wanted for the first time gets its history from --from (one request per stock). Halted days (no open) are
skipped. Safe to run again: upserts only.

    python ops/ui/push_prices.py --dry      # fetch and count, upload nothing (no UI keys needed)
    python ops/ui/push_prices.py            # needs UI_SUPABASE_URL, UI_SUPABASE_SERVICE_ROLE_KEY, KRX_ID, KRX_PW

Runs in the GitHub daily refresh after our own KRX session is published.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
JOURNAL_STOCKS = ['005930', '000660', '373220', '005380', '035420', '035720', '068270', '105560', '012450', '005490',
                  '247540', '196170']
TOP = 300
BATCH = 1000
KST = ZoneInfo("Asia/Seoul")


class Supa:
    def __init__(self):
        self.url = (os.environ.get("UI_SUPABASE_URL") or "").strip().rstrip("/")
        self.key = (os.environ.get("UI_SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        if not self.url or not self.key:
            sys.exit("UI_SUPABASE_URL and UI_SUPABASE_SERVICE_ROLE_KEY are needed")
        self.headers = {"apikey": self.key, "content-type": "application/json"}
        if self.key.startswith("eyJ"):
            self.headers["authorization"] = f"Bearer {self.key}"

    def req(self, method, path, body=None, prefer=None):
        headers = dict(self.headers, **({"prefer": prefer} if prefer else {}))
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.url}/rest/v1/{path}", data=data, headers=headers, method=method)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=60) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                if e.code < 500 or attempt == 3:
                    sys.exit(f"{method} {path.split('?')[0]} failed: {e.code} {e.read()[:300].decode(errors='replace')}")
            except urllib.error.URLError as e:
                if attempt == 3:
                    sys.exit(f"{method} {path.split('?')[0]} failed: {type(e).__name__}")
            time.sleep(2 ** attempt)

    def upsert(self, table, rows, on):
        for i in range(0, len(rows), BATCH):
            self.req("POST", f"{table}?on_conflict={on}", rows[i:i + BATCH], "resolution=merge-duplicates,return=minimal")


def num(value):
    text = str(value if value is not None else "").replace(",", "").strip()
    return float(text) if text not in ("", "-", "nan") else None


def market_day(day):
    """All KOSPI (STK) and KOSDAQ (KSQ) stocks on one session, KRX column names. Empty on a closed day."""
    from pykrx.website.krx.market.core import 전종목시세
    rows = []
    for mkt in ("STK", "KSQ"):
        df = 전종목시세().fetch(f"{day:%Y%m%d}", mkt)
        if df is None or not len(df):
            continue
        for r in df.to_dict("records"):
            if str(r.get("TDD_CLSPRC", "")).replace("-", "").strip():
                rows.append({"code": r["ISU_SRT_CD"], "name": r["ISU_ABBRV"], "mkt": r["MKT_NM"],
                             "o": num(r["TDD_OPNPRC"]), "h": num(r["TDD_HGPRC"]), "l": num(r["TDD_LWPRC"]),
                             "c": num(r["TDD_CLSPRC"]), "v": num(r["ACC_TRDVOL"]), "mktcap": num(r["MKTCAP"])})
        time.sleep(0.3)
    return rows


def history(code, start, end):
    """Unadjusted daily bars of one stock (same numbers as the daily market table)."""
    from pykrx import stock
    df = stock.get_market_ohlcv(f"{start:%Y%m%d}", f"{end:%Y%m%d}", code, adjusted=False)
    out = []
    for d, r in df.iterrows():
        o, cl = num(r.get("시가")), num(r.get("종가"))
        if not o or not cl:
            continue
        v = num(r.get("거래량"))
        out.append({"code": code, "d": d.strftime("%Y-%m-%d"), "o": o, "h": num(r.get("고가")), "l": num(r.get("저가")),
                    "c": cl, "v": int(v) if v is not None else None})
    return out


def bar(r, day):
    if not r["c"] or not r["o"]:                      # a halted day has no open; skip it
        return None
    return {"code": r["code"], "d": day.isoformat(), "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"],
            "v": int(r["v"]) if r["v"] is not None else None}


def garden_codes(sb):
    codes = set()
    for row in sb.req("GET", "garden_saves?select=body->holdings") or []:
        for h in row.get("holdings") or []:
            c = (h or {}).get("code")
            if isinstance(c, str) and c.isdigit() and len(c) == 6:
                codes.add(c)
    return codes


def sessions_after(last, today):
    from data_pipeline.market_data import due_sessions, read_calendar
    now = datetime.now(KST)
    days = due_sessions(read_calendar(os.environ.get("MARKET_CALENDAR_CSV", ROOT / "ops/live/krx_sessions.csv")),
                        now, os.getenv("MARKET_READY_AFTER_KST", "19:00"))
    return [d for d in days if last is None or d > last][-60:]      # at most ~3 months in one run


def main(argv=None):
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", default="2024-01-01", help="first day of history for a new stock")
    ap.add_argument("--dry", action="store_true", help="fetch and count only, upload nothing")
    a = ap.parse_args(argv)
    start = date.fromisoformat(a.start)
    today = datetime.now(KST).date()
    sb = None if a.dry else Supa()

    last_row = None if a.dry else (sb.req("GET", "price_daily?select=d&order=d.desc&limit=1") or [None])[0]
    last = date.fromisoformat(last_row["d"]) if last_row else None
    days = sessions_after(last, today) if not a.dry else sessions_after(None, today)[-1:]
    if not days:
        print(f"price_daily is up to date ({last})")
        return 0
    markets = {}
    for day in days:
        rows = market_day(day)
        if rows:
            markets[day] = rows
        else:
            print(f"{day}: KRX returned no rows (not uploaded; retried next run)")
    if not markets:
        sys.exit("KRX returned no data for any due session")
    latest = max(markets)
    names = [{"code": r["code"], "name": r["name"], "mkt": r["mkt"], "close": r["c"], "mktcap": r["mktcap"],
              "as_of": latest.isoformat()} for r in markets[latest]]
    top = [n["code"] for n in sorted(names, key=lambda n: -(n["mktcap"] or 0))[:TOP]]
    wanted = set(top) | set(JOURNAL_STOCKS) | (garden_codes(sb) if sb else set())
    have = set() if last is None or a.dry else {
        r["code"] for r in sb.req("GET", f"price_daily?select=code&d=eq.{last.isoformat()}&limit=5000")}
    first = min(markets)
    fresh = [] if a.dry else sorted(wanted - have)      # empty table: every wanted stock is new
    history_end = last or (first - timedelta(days=1))
    rows = [b for day, day_rows in sorted(markets.items()) for r in day_rows
            if r["code"] in wanted and (b := bar(r, day))]
    for code in fresh:                                 # new to the app: history up to the sessions fetched above
        if history_end >= start:
            rows += history(code, start, history_end)
            time.sleep(0.2)
    print(f"sessions {days[0]}..{latest} ({len(markets)} with data): {len(names)} names · {len(wanted)} stocks wanted "
          f"({len(fresh)} new) · {len(rows)} bars")
    if a.dry:
        return 0
    sb.upsert("stock_names", names, "code")
    sb.upsert("price_daily", rows, "code,d")
    print("uploaded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
