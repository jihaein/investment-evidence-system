"""서서히 verdicts for every real holding in the UI's gardens → ReThinking_UI holding_signals.

Reads the gardens (garden_saves.body.holdings: code + date of each real holding) from the UI project, looks each
(code, buy date) up in the published signals with the same query as our API (backend/api.read_signals), and
replaces the player's rows in holding_signals (ops/ui/sql/holding_signals.sql). Virtual holdings and holdings
without a six-digit code or a buy date are skipped. Stocks outside our universe are stored as not_covered so the
app can say so.

    python ops/ui/push_signals.py --dry     # read gardens and compute, write nothing
    python ops/ui/push_signals.py --holdings 095270:2025-12-01,005930:2026-01-05   # our DB only, no UI
    python ops/ui/push_signals.py           # needs SUPABASE_DB_URL, UI_SUPABASE_URL, UI_SUPABASE_SERVICE_ROLE_KEY
"""
import argparse
import os
import re
import sys
import urllib.parse
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops" / "ui"))
KST = ZoneInfo("Asia/Seoul")
BATCH = 200
KEEP = ("verdict", "fscore", "ohlson_score", "dupont_score", "sum_total", "eval_date", "baseline_date",
        "financial_as_of", "price_as_of", "rule_version", "release_id", "evidence")


def real_holdings(body):
    """(code, buy_date) of the real holdings in one saved garden, without duplicates."""
    found = []
    for h in (body or {}).get("holdings") or []:
        if not isinstance(h, dict) or h.get("virtual"):
            continue
        code, day = h.get("code"), h.get("date")
        if isinstance(code, str) and re.fullmatch(r"\d{6}", code) and isinstance(day, str) \
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            try:
                key = (code, date.fromisoformat(day))
            except ValueError:
                continue
            if key not in found:
                found.append(key)
    return found


def to_row(player, code, buy, signal, universe, today):
    row = {"player_id": player, "code": code, "buy_date": buy.isoformat(),
           "updated_at": datetime.now(KST).isoformat(), **{k: None for k in KEEP}}
    if code not in universe:
        return dict(row, status="not_covered")
    if buy > today or signal is None or "error" in signal:
        return dict(row, status="no_signal")
    row.update({k: signal.get(k) for k in KEEP})
    row["status"] = signal["status"] if signal["status"] in ("available", "held", "stale") else "available"
    return row


def compute(connection, holdings, universe, today):
    from fastapi.encoders import jsonable_encoder
    from backend.api import Holding, read_signals

    wanted = [(code, buy) for code, buy in holdings if code in universe and buy <= today]
    signals = {}
    for i in range(0, len(wanted), BATCH):
        chunk = wanted[i:i + BATCH]
        found = read_signals(connection, [Holding(stock_code=c, buy_date=b) for c, b in chunk], None)
        for (code, buy), signal in zip(chunk, found):
            signals[(code, buy)] = jsonable_encoder(signal)
    return signals


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--holdings", help="code:YYYY-MM-DD,… — look these up in our DB and print (no UI access)")
    args = ap.parse_args()
    from sqlalchemy import create_engine, text

    if args.holdings:
        ui = None
        per_player = {"(test)": real_holdings({"holdings": [
            {"code": item.split(":")[0], "date": item.split(":")[1]} for item in args.holdings.split(",") if ":" in item]})}
        args.dry = True
    else:
        from analysis_worker import UI
        ui = UI()
        gardens = ui.req("GET", "garden_saves?select=player_id,body->holdings") or []
        per_player = {g["player_id"]: real_holdings({"holdings": g.get("holdings")}) for g in gardens}
    everything = sorted({h for hs in per_player.values() for h in hs})
    today = datetime.now(KST).date()
    engine = create_engine(os.environ["SUPABASE_DB_URL"].strip(), hide_parameters=True)
    with engine.connect() as connection:
        universe = set(connection.scalars(text("SELECT stock_code FROM stocks")))
        signals = compute(connection, everything, universe, today)
    engine.dispose()
    counts = {}
    for player, holdings in per_player.items():
        rows = [to_row(player, c, b, signals.get((c, b)), universe, today) for c, b in holdings]
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            if args.holdings:
                print(r["code"], r["buy_date"], r["status"], r["verdict"],
                      f"F{r['fscore']} O{r['ohlson_score']} D{r['dupont_score']}", "eval", r["eval_date"])
        if args.dry:
            continue
        if rows:
            ui.req("POST", "holding_signals?on_conflict=player_id,code,buy_date", rows,
                   "resolution=merge-duplicates,return=minimal")
        keep = {(r["code"], r["buy_date"]) for r in rows}
        existing = ui.req("GET", f"holding_signals?select=code,buy_date&player_id=eq.{player}") or []
        for old in existing:
            if (old["code"], old["buy_date"]) not in keep:          # the holding left the garden
                ui.req("DELETE", f"holding_signals?player_id=eq.{player}&code=eq.{old['code']}"
                                 f"&buy_date=eq.{urllib.parse.quote(old['buy_date'])}", prefer="return=minimal")
    print(f"gardens {len(per_player)} · real holdings {len(everything)} · {counts}" + (" (dry run)" if args.dry else ""))


if __name__ == "__main__":
    main()
