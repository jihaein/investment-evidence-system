"""Official S4-S10 entry point; time one turn with stage models selected in project .env."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from time import perf_counter

import run_pipeline
from run_pilot import estimated_cost
from runtime_inputs import company_id, select_company_argument
from S4_investor_extraction import openai_api_client as openai_api
from S10_dialogue_generation.generate_momo_dialogue import run as run_s10

ROOT = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))
EXPECTED = {"S4": "gpt-6.2-sol", "S5": "gpt-6-luna", "S7": "gpt-6.1-terra", "S10": "gpt-6.2-sol"}


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    models = {stage: openai_api.configured_model(stage=stage) for stage in EXPECTED}
    if models != EXPECTED:
        raise ValueError("Project .env stage models differ from the authorized experiment")
    openai_api.load_key()
    if args.preflight_only:
        request = json.loads(args.input.read_text(encoding="utf-8-sig"))
        path, snapshot = select_company_argument(company_id(request), request["alert_date"])
        print(json.dumps(dict(event="preflight_passed", models=models, company_id=company_id(request),
                              alert_date=request["alert_date"], selected_company_argument=str(path),
                              as_of=snapshot["as_of"]), ensure_ascii=False), flush=True)
        return
    args.output.mkdir(parents=True, exist_ok=False)
    summary = dict(status="running", input_file=str(args.input.resolve()), stage_models=models,
                   started_at=datetime.now(KST).isoformat(),
                   timing_definition="Immediately before input JSON read through S10 dialogue.json save; setup and post-run review excluded",
                   reasoning_effort="Existing provider defaults; S4 low and prompt-cache changes are not applied",
                   pricing_source="https://developers.openai.com/api/docs/pricing")
    metrics, runtime, toulmin = None, None, None
    start = perf_counter()
    with openai_api.capture_calls() as call_recorder:
      try:
        request = json.loads(args.input.read_text(encoding="utf-8-sig"))
        summary.update(request_id=request["request_id"], company_id=company_id(request), alert_date=request["alert_date"])
        write(args.output / "input.json", request)
        print(json.dumps(dict(event="full_run_started", **summary), ensure_ascii=False), flush=True)
        toulmin, metrics = run_pipeline.run(request, args.output, call_recorder=call_recorder)
        summary["toulmin"] = str(toulmin.resolve())
        before = call_recorder.mark()
        tick = perf_counter()
        try:
            dialogue, status = run_s10(toulmin, toulmin.parent.parent / "S10/dialogue.json", model=models["S10"])
            # Capture the end boundary before writing reports or doing further inspection.
            runtime = perf_counter() - start
        finally:
            metrics["stages"]["S10"] = perf_counter() - tick
            call_recorder.tag_since(before, "S10")
        write(dialogue.with_name("validation.json"), status)
        summary.update(status="completed", total_runtime_seconds=runtime,
                       finished_at=datetime.now(KST).isoformat(), dialogue=str(dialogue.resolve()), s10_validation=status)
        print(json.dumps(dict(event="dialogue_saved", total_runtime_seconds=runtime,
                              dialogue=json.loads(dialogue.read_text(encoding="utf-8"))), ensure_ascii=False), flush=True)
      except Exception as exc:
        summary.update(status="failed", elapsed_to_failure_seconds=perf_counter() - start,
                       error_type=type(exc).__name__, error=str(exc))
        raise
      finally:
        calls = call_recorder.snapshot()
        summary.update(api_call_count=len(calls), api_calls_by_stage=dict(Counter(c.get("stage", "unknown") for c in calls)),
                       actual_models_by_stage={stage: sorted({c["model"] for c in calls if c.get("stage") == stage}) for stage in models},
                       model_selection_verified=all(c.get("stage") in models and c["model"] == models[c["stage"]] for c in calls),
                       input_tokens=sum(c.get("usage", {}).get("input_tokens", 0) for c in calls),
                       output_tokens=sum(c.get("usage", {}).get("output_tokens", 0) for c in calls),
                       estimated_cost_usd=estimated_cost(calls), api_calls=calls,
                       cost_basis="Reported usage at published model-specific Standard rates; not a billing receipt")
        if metrics is not None:
            summary.update(stage_seconds=metrics["stages"], selected_company_argument=metrics["selected_company_argument"])
            metrics.update(total_seconds=runtime if runtime is not None else perf_counter() - start,
                           api_calls=calls, stage_models=models, result=summary.get("dialogue", str(toulmin)),
                           estimated_cost_usd=summary["estimated_cost_usd"])
            metrics.update(openai_api.call_timing(calls, metrics["total_seconds"]))
            write(args.output / "turn_1_metrics.json", metrics)
            if toulmin is not None:
                write(toulmin.parent.parent / "metrics.json", metrics)
        write(args.output / "summary.json", summary)
        print(json.dumps({k: v for k, v in summary.items() if k not in ("api_calls", "s10_validation")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
