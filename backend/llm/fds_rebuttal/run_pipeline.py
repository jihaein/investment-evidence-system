"""Single entry point for one or more linked S3-through-S9 investor turns."""
import argparse
from contextlib import ExitStack, closing
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
from time import perf_counter
from unittest.mock import patch

import pipeline
from runtime_inputs import company_id, select_company_argument
from S4_final import openai_api
from S5 import graph as graph_module
from S5 import argument as argument_module
from S9.run import assemble

_SEEN_SNAPSHOTS = set()


def existing_turns(output, session):
    turns = []
    for request in Path(output).glob("*/request.sqlite"):
        with closing(sqlite3.connect(request)) as db:
            row = db.execute("SELECT session, turn FROM request LIMIT 1").fetchone()
        if row and row[0] == session:
            run_dir = request.parent
            result = run_dir / "S9/toulmin.json"
            metrics = run_dir / "metrics.json"
            turns.append({"turn_id": row[1], "run_id": run_dir.name,
                          "result": str(result) if result.is_file() else None,
                          "metrics": str(metrics) if metrics.is_file() else None})
    return sorted(turns, key=lambda entry: entry["turn_id"])


def run(request, output, *, extract=None, select=None, nuance=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    session = request.get("session_id") or request.get("request_id")
    turn = request.get("turn_id", 1)
    if not isinstance(session, str) or not session.strip() or not isinstance(turn, int) or turn < 1:
        raise ValueError("session_id/request_id and positive turn_id are required")
    metrics = {"session_id": session, "turn_id": turn, "stages": {}, "api_calls": [], "errors": [], "json_reads": 0,
               "json_cache_hits": 0, "json_read_seconds": 0, "path_search_seconds": 0}
    start = perf_counter()
    metrics["file_io_seconds"] = 0
    metrics["file_io_operations"] = 0
    cache = {}
    original_read = graph_module.read_json
    original_connect = argument_module.connect
    immutable_roots = tuple((Path.cwd() / name).resolve() for name in
                            ("S1.인과그래프", "S2.화살표노드", "S3_fianl"))
    def cached(path):
        path = Path(path)
        stamp = path.stat()
        key = (str(path.resolve()), stamp.st_mtime_ns, stamp.st_size)
        if key in cache:
            metrics["json_cache_hits"] += 1
            value = cache[key]
            return value if any(path.resolve().is_relative_to(root) for root in immutable_roots) else deepcopy(value)
        tick = perf_counter()
        value = original_read(path)
        metrics["json_reads"] += 1
        metrics["json_read_seconds"] += perf_counter() - tick
        cache[key] = value
        return value if any(path.resolve().is_relative_to(root) for root in immutable_roots) else deepcopy(value)
    def connect(*args, **kwargs):
        tick = perf_counter()
        try:
            return original_connect(*args, **kwargs)
        finally:
            metrics["path_search_seconds"] += perf_counter() - tick
    call_start = len(openai_api.CALLS)
    def diagnostic(event):
        if event.get("stage"):
            metrics.setdefault("diagnostics", []).append({k: v for k, v in event.items()
                if k not in ("raw_response", "available_refs")})
    try:
        tick = perf_counter()
        path, snapshot = select_company_argument(company_id(request), request["alert_date"])
        metrics["stages"]["S3"] = perf_counter() - tick
        metrics["selected_company_argument"] = str(path)
        metrics["company_argument_object_id"] = id(snapshot)
        snapshot_key = (str(path.resolve()), path.stat().st_mtime_ns, path.stat().st_size)
        metrics["company_argument_reused"] = snapshot_key in _SEEN_SNAPSHOTS
        _SEEN_SNAPSHOTS.add(snapshot_key)
        with ExitStack() as stack:
            for method in ("read_text", "read_bytes", "write_text", "write_bytes"):
                original = getattr(Path, method)
                def file_io(self, *a, _fn=original, **kw):
                    tick = perf_counter()
                    try:
                        return _fn(self, *a, **kw)
                    finally:
                        metrics["file_io_seconds"] += perf_counter() - tick
                        metrics["file_io_operations"] += 1
                stack.enter_context(patch.object(Path, method, file_io))
            import S5.run, S6_S7_S8.pipeline_s6_s7_s8 as p
            for module in (graph_module, S5.run, p):
                stack.enter_context(patch.object(module, "read_json", cached))
            stack.enter_context(patch.object(argument_module, "connect", connect))
            if nuance:
                real_s7 = p.run_s7
                stack.enter_context(patch.object(pipeline, "run_s7",
                    lambda *a, _s7=real_s7, **kw: _s7(*a, **kw, generate=nuance)))
            job = pipeline.create_run(request, session, turn, runs=output,
                                      last_question_cc_id=request.get("last_question_cc_id"))
            metrics["run_id"] = job.name
            # execute() already forwards S6 context to S7/S8; retain audit files and hashes.
            real_s4, real_s5, real_s6 = pipeline.run_s4, pipeline.run_s5, pipeline.run_s6
            real_s7, real_s8, real_assessment = pipeline.run_s7, pipeline.run_s8, pipeline.run_assessment
            for name, fn in [("S4", real_s4), ("S5", real_s5), ("S6", real_s6),
                             ("S7", real_s7), ("S8", real_s8), ("S8_aggregation", real_assessment)]:
                attr = {"S4":"run_s4", "S5":"run_s5", "S6":"run_s6", "S7":"run_s7",
                        "S8":"run_s8", "S8_aggregation":"run_assessment"}[name]
                def timed(*a, _fn=fn, _stage=name, **kw):
                    tick = perf_counter()
                    before = len(openai_api.CALLS)
                    try:
                        return _fn(*a, **kw)
                    finally:
                        metrics["stages"][_stage] = perf_counter() - tick
                        for record in openai_api.CALLS[before:]:
                            record["stage"] = _stage
                stack.enter_context(patch.object(pipeline, attr, timed))
            assessment = pipeline.execute(job, extract=extract, select=select, diagnostic=diagnostic)
            tick = perf_counter()
            toulmin = assemble(assessment)
            target = job / "S9/toulmin.json"
            target.parent.mkdir()
            target.write_text(json.dumps(toulmin, ensure_ascii=False, indent=2), encoding="utf-8")
            metrics["stages"]["S9"] = perf_counter() - tick
            metrics["result"] = str(target)
            return target, metrics
    except Exception as exc:
        metrics["errors"].append({"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        metrics["api_calls"] = openai_api.CALLS[call_start:]
        metrics["total_seconds"] = perf_counter() - start
        metrics["api_seconds"] = sum(call.get("seconds", 0) for call in metrics["api_calls"])
        metrics["python_seconds"] = max(0, metrics["total_seconds"] - metrics["api_seconds"])
        metrics_path = output / f"turn_{turn}_metrics.json"
        metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        if metrics.get("run_id"):
            run_metrics = output / metrics["run_id"] / "metrics.json"
            run_metrics.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest_path = output / "session.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {
            "session_id": session, "company_id": company_id(request), "alert_date": request["alert_date"],
            "turns": existing_turns(output, session)}
        if (manifest["session_id"], manifest["company_id"], manifest["alert_date"]) != (
                session, company_id(request), request["alert_date"]):
            raise ValueError("session company_id/alert_date mismatch")
        discovered = existing_turns(output, session)
        merged = {entry["turn_id"]: entry for entry in discovered}
        merged.update({entry["turn_id"]: entry for entry in manifest["turns"]})
        manifest["turns"] = [entry for entry in merged.values() if entry["turn_id"] != turn]
        manifest["turns"].append({"turn_id": turn, "run_id": metrics.get("run_id"),
                                  "result": metrics.get("result"), "metrics": str(metrics_path)})
        manifest["turns"].sort(key=lambda entry: entry["turn_id"])
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def load_env():
    """Repository .env (OPENAI_API_KEY, OPENAI_MODEL ...). Variables already set are kept."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")


def main():
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True,
                        help="Turn JSON; repeat in turn order for a session")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for source in args.input:
        request = json.loads(source.read_text(encoding="utf-8-sig"))
        print(run(request, args.output)[0])

if __name__ == "__main__":
    main()
