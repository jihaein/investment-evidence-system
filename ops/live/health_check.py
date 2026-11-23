"""Daily health check of the whole chain (run by .github/workflows/health-check.yml, started by Supabase pg_cron).

Fails (exit 1, so GitHub e-mails the owner) when any of these is wrong:
  1. our DB: the latest due KRX session is published (refresh_runs), as ops/live/check_fresh.py
  2. UI price_daily has that session
  3. UI drop_events were uploaded after that session closed
  4. UI holding_signals were refreshed in the last 2 hours (only when there are rows)
  5. GitHub: the latest daily-refresh run succeeded and started on or after that session day
  6. GitHub: analysis-worker ran successfully in the last 2 hours (Supabase pg_cron is alive)
Each check prints one line: OK / FAIL and what it saw.

    python ops/live/health_check.py           # needs SUPABASE_DB_URL, MARKET_CALENDAR_CSV, UI_SUPABASE_URL,
                                              # UI_SUPABASE_SERVICE_ROLE_KEY, GITHUB_TOKEN (actions: read), GITHUB_REPOSITORY
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops" / "ui"))
KST = ZoneInfo("Asia/Seoul")
WORKER_MAX_AGE = timedelta(hours=2)


def ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(KST)


def github_runs(workflow, token, repo):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs?per_page=10",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ies-health-check"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["workflow_runs"]


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    from sqlalchemy import create_engine, text
    from data_pipeline.market_data import due_sessions, read_calendar
    from analysis_worker import UI

    now = datetime.now(KST)
    results = []

    def check(name, ok, seen):
        results.append(ok)
        print(f"{'OK  ' if ok else 'FAIL'} {name}: {seen}")

    due = due_sessions(read_calendar(os.environ["MARKET_CALENDAR_CSV"]), now,
                       os.getenv("MARKET_READY_AFTER_KST", "19:00"))
    expected = due[-1] if due else None
    print(f"now {now:%Y-%m-%d %H:%M} KST · latest due session {expected}")

    engine = create_engine(os.environ["SUPABASE_DB_URL"].strip(), hide_parameters=True)
    with engine.connect() as connection:
        latest = connection.scalar(text("SELECT max(price_session) FROM refresh_runs WHERE reverted_at IS NULL"))
    engine.dispose()
    check("our DB published session", expected is None or (latest is not None and latest >= expected), latest)

    ui = UI()
    last_bar = (ui.req("GET", "price_daily?select=d&order=d.desc&limit=1") or [{}])[0].get("d")
    check("UI price_daily", expected is None or (last_bar is not None and last_bar >= expected.isoformat()), last_bar)

    drops = (ui.req("GET", "drop_events?select=updated_at&order=updated_at.desc&limit=1") or [{}])[0].get("updated_at")
    closed = datetime.combine(expected, datetime.min.time(), KST) + timedelta(hours=15, minutes=30) if expected else None
    check("UI drop_events uploaded after the session", expected is None or (drops is not None and ts(drops) >= closed),
          drops and f"{ts(drops):%Y-%m-%d %H:%M} KST")

    sig = (ui.req("GET", "holding_signals?select=updated_at&order=updated_at.desc&limit=1") or [])
    if sig:
        age = now - ts(sig[0]["updated_at"])
        check("UI holding_signals refreshed", age <= WORKER_MAX_AGE, f"{age.total_seconds() / 3600:.1f} h ago")
    else:
        print("skip UI holding_signals: no rows yet")

    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY", "jihaein/investment-evidence-system")
    if token:
        daily = [r for r in github_runs("daily-refresh.yml", token, repo) if r["status"] == "completed"]
        last = daily[0] if daily else None
        check("GitHub daily-refresh latest run",
              last is not None and last["conclusion"] == "success"
              and (expected is None or ts(last["created_at"]).date() >= expected),
              last and f"{last['conclusion']} · {ts(last['created_at']):%Y-%m-%d %H:%M} KST · {last['html_url']}")
        worker = [r for r in github_runs("analysis-worker.yml", token, repo) if r["status"] == "completed"]
        good = next((r for r in worker if r["conclusion"] == "success"), None)
        check("GitHub analysis-worker ran in the last 2 h",
              good is not None and now - ts(good["created_at"]) <= WORKER_MAX_AGE,
              good and f"{ts(good['created_at']):%Y-%m-%d %H:%M} KST"
              + (f" (latest run: {worker[0]['conclusion']})" if worker and worker[0] is not good else ""))
    else:
        print("skip GitHub checks: GITHUB_TOKEN not set")

    failed = results.count(False)
    print(f"{len(results) - failed}/{len(results)} checks OK")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
