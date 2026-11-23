"""Does everything the ReThinking_UI app calls exist on the UI Supabase project, and is there data where there should be?

Read-only. Uses the service key (UI_SUPABASE_URL, UI_SUPABASE_SERVICE_ROLE_KEY from .env) for tables and functions,
and the publishable key (--anon-key or UI_SUPABASE_ANON_KEY) to check that a signed-out visitor is refused where RLS
should refuse. Edge functions are called with a bad token: 401/403 means "deployed and checking sign-in", 404 means
"not deployed".

    python ops/ui/audit_ui_backend.py
    python ops/ui/audit_ui_backend.py --anon-key sb_publishable_...    # also check RLS for signed-out visitors
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# what the app reads/writes (ReThinking_UI app/ + db/functions/, 2026-11-23), who fills it, and whether rows are expected
TABLES = [
    # table,              filled by,                                        rows expected, signed-out may read
    ("price_daily",       "investment-evidence-system push_prices",        True,  False),
    ("stock_names",       "investment-evidence-system push_prices",        True,  False),
    ("holding_signals",   "investment-evidence-system push_signals",       False, False),
    ("drop_events",       "investment-evidence-system push_drops",         True,  False),
    ("analysis_results",  "investment-evidence-system analysis_worker",    False, False),
    ("analysis_inbox",    "UI bori-talk function",                         False, False),
    ("garden_saves",      "UI app (sync)",                                 True,  False),
    ("journals",          "UI app (sync)",                                 False, False),
    ("hypotheses",        "UI app (sync)",                                 False, False),
    ("players",           "UI sign-up",                                    True,  False),
    ("invite_codes",      "UI admin (Table Editor)",                       True,  False),
    ("invite_uses",       "UI redeem_invite",                              True,  False),
    ("llm1_docs",         "UI bori-talk function",                         False, False),
    ("llm1_ops",          "UI bori-talk function",                         False, False),
    ("planets",           "UI observatory",                                False, None),
    ("planet_neighbours", "UI observatory",                                False, None),
    ("expedition_week",   "UI observatory",                                False, None),
    ("expedition_runs",   "UI expedition-submit function",                 False, False),
    ("mentors",           "UI content",                                    False, None),
]
RPCS = ["has_invite", "redeem_invite", "is_master", "delete_me", "llm_take", "friend_talk_take"]
FUNCTIONS = ["bori-talk", "friend-chat", "jigi-llm", "portfolio-read", "quest-content", "quest-play", "quest-settle",
             "expedition-submit"]


def call(url, key, method="GET", body=None, bearer=None, prefer=None):
    headers = {"apikey": key, "content-type": "application/json"}
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
    if prefer:
        headers["prefer"] = prefer
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()
    except Exception as e:                                   # network
        return None, {}, str(e).encode()


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--anon-key", default=os.environ.get("UI_SUPABASE_ANON_KEY"))
    args = ap.parse_args()
    url = (os.environ.get("UI_SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("UI_SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        sys.exit("UI_SUPABASE_URL and UI_SUPABASE_SERVICE_ROLE_KEY are needed (.env)")
    bearer = key if key.startswith("eyJ") else None
    problems = []

    print("== tables (service key: exists, row count)")
    for name, by, want_rows, public in TABLES:
        status, headers, raw = call(f"{url}/rest/v1/{name}?select=*&limit=1", key, bearer=bearer, prefer="count=exact")
        if status not in (200, 206):                          # 206: a partial page of a counted read, fine
            msg = raw[:120].decode(errors="replace")
            print(f"FAIL {name:18} HTTP {status} {msg}")
            problems.append(f"table {name} missing or unreadable ({status})")
            continue
        total = (headers.get("content-range") or "*/?").split("/")[-1]
        empty = total in ("0", "?")
        mark = "WARN" if want_rows and empty else "OK  "
        if want_rows and empty:
            problems.append(f"table {name} is empty (filled by {by})")
        print(f"{mark} {name:18} rows {total:>8}   ← {by}")
        if args.anon_key and public is False:
            s2, _, raw2 = call(f"{url}/rest/v1/{name}?select=*&limit=1", args.anon_key)
            leaked = s2 in (200, 206) and raw2.strip() not in (b"[]", b"")
            if leaked:
                problems.append(f"table {name}: a signed-out visitor can read rows (RLS)")
            print(f"     {'FAIL' if leaked else 'ok'} signed-out read → HTTP {s2}{' ROWS VISIBLE' if leaked else ''}")

    print("== RPC functions (listed in the API schema)")
    status, _, raw = call(f"{url}/rest/v1/", key, bearer=bearer)
    try:
        paths = json.loads(raw).get("paths", {}) if status == 200 else {}
    except ValueError:
        paths = {}
    if not paths:
        print(f"FAIL could not read the API schema (HTTP {status})")
        problems.append("API schema unreadable")
    for name in RPCS:
        exists = f"/rpc/{name}" in paths
        print(f"{'OK  ' if exists else 'FAIL'} {name}")
        if paths and not exists:
            problems.append(f"rpc {name} missing")

    print("== edge functions (called with a bad token: 401/403 = deployed, 404 = not deployed)")
    for name in FUNCTIONS:
        status, _, raw = call(f"{url}/functions/v1/{name}", key, "POST", {}, bearer="not-a-user-token")
        ok = status in (400, 401, 403, 405, 422)
        print(f"{'OK  ' if ok else 'FAIL'} {name:18} HTTP {status} {'' if ok else raw[:120].decode(errors='replace')}")
        if not ok:
            problems.append(f"function {name}: HTTP {status}")

    print()
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(" -", p)
        sys.exit(1)
    print("all present")


if __name__ == "__main__":
    main()
