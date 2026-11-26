"""Analyse the UI's 보리/루온 conversations with 윤정민's rebuttal pipeline and store the results for the UI.

Flow: ReThinking_UI analysis_inbox (packet v2.0: request_id, company_id, conversation[{question, answer}])
      → backend/llm/fds_rebuttal/run_mixed_pipeline.py (S4 extraction … S9 Toulmin … S10 dialogue)
      → analysis_results (ReThinking_UI db/migrations/012, 020): result = the S9 Toulmin JSON, dialogue = the S10 line
        루온 says in the app ({character_message, emotion_code, asked_cc_id} + how it was made)
      → analysis_inbox.analyzed_at

    python ops/ui/analysis_worker.py                       # every pending packet (needs UI_SUPABASE_* and OPENAI_API_KEY)
    python ops/ui/analysis_worker.py --packet p.json       # one packet file, no database: prints the result summary
    python ops/ui/analysis_worker.py --packet p.json --alert-date 2026-11-20

Each packet is turn 1 of its own session (a correction arrives as a new request_id with supersedes_request_id).
alert_date = the packet's KST arrival date unless given; it picks the S3 snapshot in effect on that day.
A failed run is retried on the next run, up to MAX_ATTEMPTS; then it is stored as failed and the packet is closed.
"""
import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "backend" / "llm" / "fds_rebuttal"
KST = ZoneInfo("Asia/Seoul")
MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = int(os.environ.get("REBUTTAL_TIMEOUT_SECONDS") or 900)   # one packet, S4 through S10


def stage_models():
    """The stage models run_mixed_pipeline.py accepts (its EXPECTED); it refuses to run with any other set, so the
    worker passes exactly these unless OPENAI_MODEL_<stage> is already set in the environment."""
    tree = ast.parse((PIPELINE / "run_mixed_pipeline.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "EXPECTED" for t in node.targets):
            return ast.literal_eval(node.value)
    raise RuntimeError("run_mixed_pipeline.py has no EXPECTED stage models")


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
    """Run S4-S10 in its own process (the pipeline imports its stages from its folder).
    Returns (S9 Toulmin result, S10 dialogue record)."""
    source = Path(workdir) / "request.json"
    source.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    output = Path(workdir) / "run"                            # run_mixed_pipeline.py creates it (must not exist)
    env = dict(os.environ)
    for stage, model in stage_models().items():
        env.setdefault(f"OPENAI_MODEL_{stage}", model)
    proc = subprocess.run([sys.executable, "run_mixed_pipeline.py", "--input", str(source), "--output", str(output)],
                          cwd=PIPELINE, env=env, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    summary_path = output / "summary.json"
    run = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    if proc.returncode != 0 or run.get("status") != "completed":
        if run.get("error"):
            raise RuntimeError(f"{run.get('error_type', 'Error')}: {run['error']}"[:500])
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(tail[0][:500])
    return read_run(run)


def read_run(run):
    """The S9 result and the S10 record from a completed run_mixed_pipeline.py summary."""
    toulmin = json.loads(Path(run["toulmin"]).read_text(encoding="utf-8"))
    line = json.loads(Path(run["dialogue"]).read_text(encoding="utf-8"))
    status = run.get("s10_validation") or {}
    if not isinstance(line.get("character_message"), str) or not line["character_message"].strip():
        raise RuntimeError("S10 dialogue has no character_message")
    dialogue = {"character_message": line["character_message"].strip(), "emotion_code": line.get("emotion_code"),
                "asked_cc_id": line.get("asked_cc_id"), "speaker": "luon", "persona_id": "luon_v1",
                "fallback": bool(status.get("fallback")), "attempts": status.get("attempts"),
                "scope_case": status.get("scope_case"),
                "fact_ids": sorted({f for c in (status.get("citations") or {}).values() for f in c.get("fact_ids", [])}),
                "models": run.get("stage_models"), "seconds": run.get("total_runtime_seconds"),
                "estimated_cost_usd": run.get("estimated_cost_usd")}
    return toulmin, dialogue


def item_tag(item):
    """The S8 tag of one claim/ground mention: its evaluation, or the reason it has none."""
    ev = item.get("evaluation")
    if isinstance(ev, dict):
        return ev.get("tag")
    if isinstance(ev, list) and ev:
        return ",".join(sorted({e.get("tag") for e in ev if isinstance(e, dict) and e.get("tag")})) or None
    return f"({item.get('mapping_status') or 'no evaluation'})"


def summary(result, dialogue=None):
    return {"luon": (dialogue or {}).get("character_message"), "emotion": (dialogue or {}).get("emotion_code"),
            "decision": result.get("decision"), "decided_by_rule": result.get("decided_by_rule"),
            "as_of": result.get("as_of"),
            "claims": [{"span": c.get("span"), "tag": item_tag(c)} for c in result.get("claim", [])],
            "grounds": [{"span": g.get("span"), "tag": item_tag(g)} for g in result.get("grounds", [])],
            "backing": len(result.get("backing") or []), "rebuttal": len(result.get("rebuttal") or []),
            "unresolved": len(result.get("unresolved_items") or []),
            "next_questions": [q.get("text") for q in (result.get("question_candidates") or [])][:3]}


def kst_date(timestamp):
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(KST).date().isoformat()


def alert_date_of(packet, received_at):
    """The day of the loss signal that started the talk: the packet's alert_date (YYYY-MM-DD, asked of the UI team
    2026-11-23) when it is there and valid, otherwise the day the packet arrived (KST)."""
    value = packet.get("alert_date") if isinstance(packet, dict) else None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            pass
    return kst_date(received_at)


def check_tables(ui):
    """Stop before spending on the LLM when the UI database has not got the dialogue column yet."""
    try:
        ui.req("GET", "analysis_results?select=request_id,dialogue&limit=1")
    except RuntimeError as exc:
        sys.exit(f"analysis_results.dialogue is missing: run ReThinking_UI db/migrations/020_analysis_dialogue.sql ({exc})")


def process_inbox(ui, limit):
    check_tables(ui)
    rows = ui.req("GET", "analysis_inbox?select=request_id,player_id,route,payload,received_at"
                         f"&analyzed_at=is.null&order=received_at.asc&limit={limit}") or []
    print(f"pending packets: {len(rows)}")
    for row in rows:
        rid = row["request_id"]
        prior = (ui.req("GET", f"analysis_results?select=attempts&request_id=eq.{urllib.parse.quote(rid)}") or [{}])[0]
        attempts = (prior.get("attempts") or 0) + 1
        packet = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        alert = alert_date_of(packet, row["received_at"])
        record = {"request_id": rid, "player_id": row["player_id"], "company_id": packet.get("company_id"),
                  "alert_date": alert, "attempts": attempts, "analyzed_at": datetime.now(KST).isoformat()}
        close = True
        if row["route"] == "insufficient_input":
            record.update(status="insufficient_input", error=None)
        else:
            try:
                with tempfile.TemporaryDirectory(prefix="rebuttal-") as work:
                    result, dialogue = run_pipeline(request_from_packet(packet, alert), work)
                record.update(status="done", decision=result.get("decision"), result=result, dialogue=dialogue,
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
    ap.add_argument("--out", type=Path, help="with --packet: also save the whole S9 result and the S10 line here")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()
    if args.packet:
        packet = json.loads(args.packet.read_text(encoding="utf-8"))
        alert = args.alert_date or datetime.now(KST).date().isoformat()
        with tempfile.TemporaryDirectory(prefix="rebuttal-") as work:
            result, dialogue = run_pipeline(request_from_packet(packet, alert), work)
        if args.out:
            args.out.write_text(json.dumps({"result": result, "dialogue": dialogue}, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        print(json.dumps(summary(result, dialogue), ensure_ascii=False, indent=1))
        return
    process_inbox(UI(), args.limit)


if __name__ == "__main__":
    main()
