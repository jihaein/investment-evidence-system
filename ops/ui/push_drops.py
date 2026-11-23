"""급락 감지 for the 120 stocks, every trading day → ReThinking_UI drop_events.

The rule is models/sudden/events.py, unchanged (설계서 v3 2절): AR = the stock's return minus its market index's
return, z = AR / σ with σ the standard deviation of AR over D-120..D-1, and a day with z <= -1.96 is a drop day. A drop
within 10 trading days of the first one belongs to the same event. Corporate-action days and the first day after a
trading halt are not judged.

What is different from the research run: the daily DB (prices) has no exchange 등락률 or volume, so
  - a day's return is the close-to-close return,
  - a corporate-action day is a day the listed share count changed (instead of 등락률 vs close return),
  - a halt is a session missing from the stock's rows (instead of volume 0),
  - a day beyond the ±30% daily price limit is not judged either: such a move cannot be trading, so it is a base-price
    reset (split, merger, capital reduction, relisting) the share count did not show on that day.
The market index comes from pykrx with the KRX login (KRX_ID, KRX_PW), as events.py does.

Rows are replaced for the recent window only (RECENT_DAYS); older events are dropped from the UI table.

    python ops/ui/push_drops.py --dry            # compute and print, write nothing
    python ops/ui/push_drops.py                  # needs SUPABASE_DB_URL, KRX_ID/KRX_PW, UI_SUPABASE_URL, UI_SUPABASE_SERVICE_ROLE_KEY
    python ops/ui/push_drops.py --no-market      # without the index (market_adjusted = false; for a local look only)
"""
import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "models" / "sudden"))
sys.path.insert(0, str(ROOT / "ops" / "ui"))
import events as E  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
RULE_VERSION = "sudden-events-v1-daily"
PRICE_LIMIT = 0.30           # KRX daily price limit; beyond it (plus rounding) a close-to-close move is not trading
LIMIT_SLACK = 0.005
LOOKBACK_DAYS = 320          # calendar days of prices read: ~210 sessions, enough for σ (120) before the recent window
RECENT_DAYS = 90             # events whose first drop day D is within this many calendar days are kept in the UI


def read_prices(connection, since):
    from sqlalchemy import text
    rows = connection.execute(text("""
        SELECT p.stock_code, s.stock_name, COALESCE(p.market, s.market) AS market, p.bas_dd, p.close_prc,
               p.mktcap, p.list_shrs
        FROM prices p JOIN stocks s USING (stock_code)
        WHERE p.bas_dd >= :since AND p.close_prc IS NOT NULL AND p.close_prc > 0
        ORDER BY p.stock_code, p.bas_dd
    """), {"since": since}).mappings().all()
    return pd.DataFrame([dict(r) for r in rows],
                        columns=["stock_code", "stock_name", "market", "bas_dd", "close_prc", "mktcap", "list_shrs"])


def prepare(raw, calendar):
    """events.py's price frame from DB rows: one row per stock per session of the calendar between its first and
    last row. A missing session is a halt (volume 0, no close); a changed share count marks a corporate action."""
    cal = pd.DatetimeIndex(sorted(pd.to_datetime(pd.Series(calendar)).unique()))
    out = []
    for code, g in raw.groupby("stock_code", sort=True):
        g = g.assign(trade_date=pd.to_datetime(g["bas_dd"])).drop_duplicates("trade_date").set_index("trade_date")
        days = cal[(cal >= g.index.min()) & (cal <= g.index.max())]
        if len(days) == 0:
            continue
        f = g.reindex(days)
        present = f["close_prc"].notna()
        close = pd.to_numeric(f["close_prc"], errors="coerce")
        shares = pd.to_numeric(f["list_shrs"], errors="coerce")
        prev_close = close.where(present).ffill().shift(1)
        prev_shares = shares.where(present & shares.notna()).ffill().shift(1)
        ret = (close / prev_close - 1).where(present)
        beyond_limit = ret.abs() > PRICE_LIMIT + LIMIT_SLACK
        frame = pd.DataFrame({
            "stock_code": code,
            "company_name": g["stock_name"].dropna().iloc[-1] if g["stock_name"].notna().any() else code,
            "market": E.market_of(g["market"].dropna().iloc[-1] if g["market"].notna().any() else "KOSDAQ"),
            "trade_date": days,
            "close": close.to_numpy(),
            "ret": ret.to_numpy(),
            "volume": present.astype(int).to_numpy(),
            "mktcap": pd.to_numeric(f["mktcap"], errors="coerce").to_numpy(),
            "corp_action_hint": ((present & shares.notna() & prev_shares.notna() & (shares != prev_shares))
                                 | (present & beyond_limit)).to_numpy(),
        })
        out.append(frame)
    if not out:
        return pd.DataFrame(columns=["stock_code", "company_name", "market", "trade_date", "close", "ret", "volume",
                                     "mktcap", "corp_action_hint"])
    return pd.concat(out, ignore_index=True)


def index_returns(index_close):
    """trade_date, market, close → trade_date, market, index_ret (as events.load_index)."""
    idx = index_close.copy()
    idx["trade_date"] = pd.to_datetime(idx["trade_date"])
    idx["market"] = idx["market"].map(E.market_of)
    idx = idx.sort_values(["market", "trade_date"])
    idx["index_ret"] = idx.groupby("market")["close"].pct_change()
    return idx[["trade_date", "market", "index_ret"]]


def detect(prices, index, since, until):
    found, _, candidates = E.detect_events(prices, index, apply_mktcap=False,
                                           start=pd.Timestamp(since), end=pd.Timestamp(until))
    return found, candidates


def to_rows(found, market_adjusted):
    now = datetime.now(KST).isoformat()
    rows = []
    for r in found.to_dict("records"):
        rows.append({
            "code": r["stock_code"], "drop_date": r["D"], "last_hit_date": r["last_hit_date"],
            "n_hit_days": int(r["n_hit_days"]), "ret_pct": r["ret_pct"], "index_ret_pct": r["index_ret_pct"],
            "ar_pct": r["ar_pct"], "sigma_ar_pct": r["sigma_ar_pct"], "z": r["z"],
            "market_adjusted": bool(market_adjusted), "rule_version": RULE_VERSION, "updated_at": now,
        })
    return rows


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry", action="store_true", help="compute and print, write nothing")
    ap.add_argument("--no-market", action="store_true", help="no index (market_adjusted = false)")
    ap.add_argument("--recent-days", type=int, default=RECENT_DAYS)
    args = ap.parse_args()
    from sqlalchemy import create_engine

    today = datetime.now(KST).date()
    since = today - timedelta(days=LOOKBACK_DAYS)
    engine = create_engine(os.environ["SUPABASE_DB_URL"].strip(), hide_parameters=True)
    with engine.connect() as connection:
        from sqlalchemy import text
        universe = set(connection.scalars(text("SELECT stock_code FROM stocks")))
        raw = read_prices(connection, since)
    engine.dispose()
    if raw.empty:
        sys.exit("no prices in the DB for the lookback window")
    latest = pd.to_datetime(raw["bas_dd"]).max().date()
    index = None
    if not args.no_market:
        markets = sorted({E.market_of(m) for m in raw["market"].dropna()})
        index = index_returns(E.fetch_index_pykrx(markets, pd.Timestamp(since) - timedelta(days=14), pd.Timestamp(latest)))
        if index["trade_date"].max().date() < latest:
            print(f"warning: index ends {index['trade_date'].max().date()}, prices end {latest}; "
                  "the days after cannot be judged yet")
    calendar = index["trade_date"] if index is not None else pd.to_datetime(raw["bas_dd"])
    prices = prepare(raw, calendar)
    window_start = latest - timedelta(days=args.recent_days)
    found, candidates = detect(prices, index, window_start, latest)
    rows = to_rows(found, index is not None)
    excluded = candidates[candidates["excluded"] != ""] if len(candidates) else candidates
    if args.dry:
        print(f"stocks in the DB: {len(universe)} · with prices in the window: {raw['stock_code'].nunique()}"
              + (f" · none: {', '.join(sorted(universe - set(raw['stock_code'])))}" if universe - set(raw['stock_code']) else ""))
    print(f"stocks {prices['stock_code'].nunique()} · sessions to {latest} · window from {window_start} · "
          f"events {len(rows)} · excluded drop days {len(excluded)}" + (" (dry run)" if args.dry else ""))
    for r in rows:
        print(r["code"], r["drop_date"], f"z {r['z']}", f"AR {r['ar_pct']}%", f"ret {r['ret_pct']}%",
              f"days {r['n_hit_days']}", "" if r["market_adjusted"] else "(no market)")
    for r in excluded.to_dict("records") if len(excluded) else []:
        print("excluded", r["stock_code"], r["trade_date"], r["excluded"])
    if args.dry:
        return
    from analysis_worker import UI
    ui = UI()
    if rows:
        ui.req("POST", "drop_events?on_conflict=code,drop_date", rows, "resolution=merge-duplicates,return=minimal")
    keep = {(r["code"], r["drop_date"]) for r in rows}
    existing = ui.req("GET", "drop_events?select=code,drop_date") or []
    gone = [e for e in existing if (e["code"], e["drop_date"]) not in keep]
    for e in gone:        # older than the window, or no longer a drop after a data correction
        ui.req("DELETE", f"drop_events?code=eq.{e['code']}&drop_date=eq.{e['drop_date']}", prefer="return=minimal")
    print(f"uploaded {len(rows)} · removed {len(gone)}")


if __name__ == "__main__":
    main()
