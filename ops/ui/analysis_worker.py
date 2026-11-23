"""Analyse the UI's 보리 conversations with 윤정민's rebuttal pipeline and store the results for the UI.

Flow: ReThinking_UI analysis_inbox (packet v2.0: request_id, company_id, conversation[{question, answer}])
      → backend/llm/fds_rebuttal/run_pipeline.py (S4 extraction … S9 Toulmin)
      → analysis_results (ops/ui/sql/analysis_results.sql) and analysis_inbox.analyzed_at

    python ops/ui/analysis_worker.py                       # every pending packet (needs UI_SUPABASE_* and OPENAI_API_KEY)
    python ops/ui/analysis_worker.py --packet p.json       # one packet file, no database: prints the result summary
    python ops/ui/analysis_worker.py --packet p.json --alert-date 2026-11-20

Each packet is turn 1 of its own session (a correction arrives as a new request_id with supersedes_request_id).
alert_date = the packet's KST arrival date unless given; it picks the S3 snapshot in effect on that day.
A failed run is retried on the next run, up to MAX_ATTEMPTS; then it is stored as failed and the packet is closed.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "backend" / "llm" / "fds_rebuttal"
KST = ZoneInfo("Asia/Seoul")
MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = 600


class UI:
    def __init__(self):
        self.url = (os.environ.get("UI_SUPABASE_URL") or "").strip().rstrip("/")
        key = (os.environ.get("UI_SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        if not self.url or not key:
            sys.exit("UI_SUPABASE_URL and UI_SUPABASE_SERVICE_ROLE_KEY are needed")
        self.headers = {"apikey": key, "content-type": "application/json"}
        if key.startswith("eyJ"):
            self.headers["authorization"] = f"Bearer {key}"

    def req(self, method, path, body=None, prefer=None):
        headers = dict(self.headers, **({"prefer": prefer} if prefer else {}))
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        request = urllib.request.Request(f"{self.url}/rest/v1/{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path.split('?')[0]}: HTTP {e.code} {e.read()[:300].decode(errors='replace')}")


def request_from_packet(packet, alert_date):
    if packet.get("schema_version") != "2.0":
        raise ValueError(f"unsupported packet schema {packet.get('schema_version')}")
    code = packet.get("company_id")
    if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
        raise ValueError("packet has no six-digit company_id")
    conversation = [{"question": t.get("question"), "answer": t["answer"]} for t in packet.get("conversation") or []
                    if isinstance(t, dict) and isinstance(t.get("answer"), str) and t["answer"].strip()]
    if not conversation:
        raise ValueError("packet conversation has no answers")
    return {"request_id": packet["request_id"], "session_id": packet["request_id"], "turn_id": 1,
            "company_id": code, "alert_date": alert_date, "conversation": conversation}


def run_pipeline(request, workdir):
    """Run the pipeline in its own process (it imports its stages from its folder). Returns the S9 result."""
    source = Path(workdir) / "request.json"
    source.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    output = Path(workdir) / "runs"
    proc = subprocess.run([sys.executable, "run_pipeline.py", "--input", str(source), "--output", str(output)],
                          cwd=PIPELINE, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(tail[0][:500])
    result = Path(proc.stdout.strip().splitlines()[-1])
    return json.loads(result.read_text(encoding="utf-8"))


def summary(result):
    return {"decision": result.get("decision"), "decided_by_rule": result.get("decided_by_rule"),
            "as_of": result.get("as_of"), "claims": [
                {"span": c.get("span"), "tag": (c.get("evaluation") or {}).get("tag")} for c in result.get("claim", [])],
            "grounds": [{"span": g.get("span"), "tag": (g.get("evaluation") or {}).get("tag")}
                        for g in result.get("grounds", [])]}


def kst_date(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(KST).date().isoformat()


def process_inbox(ui, limit):
    rows = ui.req("GET", "analysis_inbox?select=request_id,player_id,route,payload,received_at"
                         f"&analyzed_at=is.null&order=received_at.asc&limit={limit}") or []
    print(f"pending packets: {len(rows)}")
    for row in rows:
        rid = row["request_id"]
        prior = (ui.req("GET", f"analysis_results?select=attempts&request_id=eq.{urllib.parse.quote(rid)}") or [{}])[0]
        attempts = (prior.get("attempts") or 0) + 1
        packet = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        alert = kst_date(row["received_at"])
        record = {"request_id": rid, "player_id": row["player_id"], "company_id": packet.get("company_id"),
                  "alert_date": alert, "attempts": attempts, "analyzed_at": datetime.now(KST).isoformat()}
        close = True
        if row["route"] == "insufficient_input":
            record.update(status="insufficient_input", error=None)
        else:
            try:
                with tempfile.TemporaryDirectory(prefix="rebuttal-") as work:
                    result = run_pipeline(request_from_packet(packet, alert), work)
                record.update(status="done", decision=result.get("decision"), result=result,
                              as_of=result.get("as_of"), error=None)
            except Exception as exc:                          # keep going with the other packets
                record.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:500])
                close = attempts >= MAX_ATTEMPTS or isinstance(exc, ValueError)
        ui.req("POST", "analysis_results?on_conflict=request_id", [record], "resolution=merge-duplicates,return=minimal")
        if close:
            ui.req("PATCH", f"analysis_inbox?request_id=eq.{urllib.parse.quote(rid)}",
                   {"analyzed_at": datetime.now(KST).isoformat()}, "return=minimal")
        print(f"{rid}: {record['status']}" + (f" ({record.get('decision')})" if record.get("decision") else "")
              + (f" · {record['error']}" if record.get("error") else "") + ("" if close else " · will retry"))


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--packet", type=Path, help="analyse one packet file (no database)")
    ap.add_argument("--alert-date", help="YYYY-MM-DD for --packet (default today, KST)")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()
    if args.packet:
        packet = json.loads(args.packet.read_text(encoding="utf-8"))
        alert = args.alert_date or datetime.now(KST).date().isoformat()
        with tempfile.TemporaryDirectory(prefix="rebuttal-") as work:
            result = run_pipeline(request_from_packet(packet, alert), work)
        print(json.dumps(summary(result), ensure_ascii=False, indent=1))
        return
    process_inbox(UI(), args.limit)


if __name__ == "__main__":
    main()
