#!/bin/bash
# Daily KRX/DART refresh for launchd. Runs one tick, logs it, and alerts on failure.
REPO="/Users/jihaein/Developer/investment-evidence-system"
LOGDIR="$REPO/ops/live/logs"
LOCK="$REPO/ops/live/.run.lock"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/refresh_$(date +%Y-%m-%d).log"

notify() {
  /usr/bin/osascript -e "display notification \"$2\" with title \"IES 데이터 갱신\" subtitle \"$1\" sound name \"Basso\"" >/dev/null 2>&1
}

# Skip if a previous run is still going (mkdir is atomic).
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +240 2>/dev/null)" ]; then
    rmdir "$LOCK" 2>/dev/null; mkdir "$LOCK" || exit 0   # stale lock older than 4h
  else
    echo "$(date '+%F %T') previous run still active, skipping" >> "$LOG"; exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

cd "$REPO" || { notify "실패" "저장소 폴더를 찾을 수 없음"; exit 1; }
echo "===== $(date '+%F %T') start" >> "$LOG"
# 1) Fill in anything missing on this computer from cloud storage (never overwrites local files).
if ! "$REPO/.venv/bin/python" -u ops/cloud/sync.py pull >> "$LOG" 2>&1; then
  notify "클라우드 복원 실패" "ops/cloud/sync.py pull 오류. 로그 확인"
fi
# 2) Refresh KRX/DART and publish.
"$REPO/.venv/bin/python" -u -m data_pipeline.scheduled_refresh --once >> "$LOG" 2>&1
status=$?
echo "===== $(date '+%F %T') exit $status" >> "$LOG"
# 3) Save everything (new prices, filings, snapshots, logs) to cloud storage, even if the refresh failed.
if ! "$REPO/.venv/bin/python" -u ops/cloud/sync.py push >> "$LOG" 2>&1; then
  notify "클라우드 업로드 실패" "데이터가 클라우드에 저장되지 않음. 로그 확인"
  [ $status -eq 0 ] && status=4
fi

if [ $status -ne 0 ]; then
  reason=$(grep -E "refresh failed|Error|error" "$LOG" | tail -1 | cut -c1-120 | tr '"' "'")
  notify "실패 (exit $status)" "${reason:-로그 확인: $LOG}"
  exit $status
fi

# Staleness check: the latest published session must be today's (after 19:00) or the previous one.
"$REPO/.venv/bin/python" - >> "$LOG" 2>&1 <<'PY'
import os, sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
sys.path.insert(0, os.getcwd())
from data_pipeline.market_data import due_sessions, read_calendar
load_dotenv(".env")
now = datetime.now(ZoneInfo("Asia/Seoul"))
due = due_sessions(read_calendar(os.environ["MARKET_CALENDAR_CSV"]), now, os.getenv("MARKET_READY_AFTER_KST", "19:00"))
with create_engine(os.environ["SUPABASE_DB_URL"], hide_parameters=True).connect() as c:
    latest = c.scalar(text("SELECT max(price_session) FROM refresh_runs WHERE reverted_at IS NULL"))
print(f"staleness check: published={latest} expected={due[-1] if due else None}")
sys.exit(3 if due and (latest is None or latest < due[-1]) else 0)
PY
if [ $? -eq 3 ]; then
  notify "지연" "최신 거래일 시세가 아직 반영되지 않음. 로그 확인"
  exit 3
fi
echo "$(date '+%F %T') ok" >> "$LOG"
