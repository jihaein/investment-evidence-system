"""Legacy S4-S10 pilot helper. The official measured entry point is run_mixed_pipeline.py."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from time import perf_counter

import run_pipeline
from runtime_inputs import card_bundle, company_id, select_company_argument
from S4_investor_extraction import openai_api_client as openai_api
from S10_dialogue_generation.generate_momo_dialogue import run as run_s10

ROOT = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))
MODEL_RATES = {
    # Standard USD per 1M tokens: input, cached input, 30m write, 12h write, output.
    "gpt-6-luna": (.10, .01, .125, .25, .50),
    "gpt-6.1-terra": (1.00, .05, 1.25, 2.50, 5.00),
    "gpt-6.2-sol": (2.00, .10, 2.50, 5.00, 10.00),
}


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def estimated_cost(calls):
    total = 0.0
    for call in calls:
        input_rate, cached_rate, short_write_rate, long_write_rate, output_rate = MODEL_RATES[call["model"]]
        usage = call.get("usage", {})
        inp, out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        details = usage.get("input_tokens_details", {})
        cached = details.get("cached_tokens", 0)
        written = details.get("cache_write_tokens", 0)
        writes = details.get("cache_write_tokens_details", {})
        long_writes = writes.get("ttl_12h_tokens", 0)
        short_writes = max(0, written - long_writes)
        input_factor, output_factor = (2, 1.5) if inp > 272000 else (1, 1)
        amount = ((max(0, inp - cached - written) * input_rate + cached * cached_rate
                   + short_writes * short_write_rate + long_writes * long_write_rate) * input_factor
                  + out * output_rate * output_factor) / 1_000_000
        call["estimated_cost_usd"] = amount
        total += amount
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    request = json.loads(args.input.read_text(encoding="utf-8-sig"))
    model = openai_api.configured_model()
    if model not in MODEL_RATES:
        raise ValueError("Pilot model must have verified pricing in MODEL_RATES")
    if not openai_api.load_key():
        raise ValueError("Missing API key")
    snapshot_path, snapshot = select_company_argument(company_id(request), request["alert_date"])
    cards, _ = card_bundle(ROOT / "S2_edge_cards/edge_cards")
    summary = dict(input_file=str(args.input.resolve()), request_id=request["request_id"],
                   company_id=company_id(request), alert_date=request["alert_date"], model=model,
                   selected_company_argument=str(snapshot_path), as_of=snapshot["as_of"],
                   effective_from=snapshot["effective_from"], effective_to=snapshot["effective_to"],
                   card_review_status_counts=dict(Counter(c["review_status"] for c in cards.values())),
                   observed_nodes=sum(n["status"] == "observed" for n in snapshot["companies"][company_id(request)]["nodes"].values()))
    print(json.dumps(dict(event="preflight_passed", **summary), ensure_ascii=False), flush=True)
    if args.preflight_only:
        return
    args.output.mkdir(parents=True, exist_ok=False)
    write(args.output / "input.json", request)
    write(args.output / "preflight.json", summary)
    summary.update(started_at=datetime.now(KST).isoformat(), status="running")
    start = perf_counter()
    metrics = None
    s10_calls = []
    try:
        toulmin, metrics = run_pipeline.run(request, args.output)
        summary["toulmin"] = str(toulmin)
        tick = perf_counter()
        with openai_api.capture_calls() as recorder:
            try:
                dialogue, validation = run_s10(toulmin, toulmin.parent.parent / "S10/dialogue.json")
            finally:
                metrics["stages"]["S10"] = perf_counter() - tick
                recorder.tag_since(0, "S10")
                s10_calls = recorder.snapshot()
        write(dialogue.with_name("validation.json"), validation)
        assessment = json.loads((toulmin.parent.parent / "S8/assessment.json").read_text(encoding="utf-8"))
        s7 = json.loads((toulmin.parent.parent / "S7/tagging_llm.json").read_text(encoding="utf-8"))
        summary.update(status="completed", dialogue=str(dialogue), assessment_summary=assessment["summary"],
                       s7_metadata=s7["metadata"], s7_failed_items=s7["failed_items"], s10_validation=validation)
        print(json.dumps(dict(event="dialogue_created", dialogue=json.loads(dialogue.read_text(encoding="utf-8"))), ensure_ascii=False), flush=True)
    except Exception as exc:
        summary.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        calls = ([*metrics.get("api_calls", []), *s10_calls] if metrics is not None else s10_calls)
        summary.update(finished_at=datetime.now(KST).isoformat(), total_seconds=perf_counter() - start,
                       api_call_count=len(calls), models_used=sorted({c["model"] for c in calls}),
                       api_calls_by_stage=dict(Counter(c.get("stage", "unknown") for c in calls)),
                       total_input_tokens=sum(c.get("usage", {}).get("input_tokens", 0) for c in calls),
                       total_output_tokens=sum(c.get("usage", {}).get("output_tokens", 0) for c in calls),
                       estimated_cost_usd=estimated_cost(calls),
                       cost_basis="Published model-specific Standard token rates; not a billing receipt",
                       pricing_source=f"https://developers.openai.com/api/docs/models/{model}",
                       api_calls=calls)
        write(args.output / "pilot_summary.json", summary)
        if metrics is not None:
            metrics.update(api_calls=calls, total_seconds=summary["total_seconds"],
                           result=summary.get("dialogue", summary.get("toulmin")),
                           estimated_cost_usd=summary["estimated_cost_usd"])
            metrics.update(openai_api.call_timing(calls, metrics["total_seconds"]))
            write(args.output / "turn_1_metrics.json", metrics)
            write(Path(summary["toulmin"]).parent.parent / "metrics.json", metrics)
        print(json.dumps({k: v for k, v in summary.items() if k not in ("api_calls", "s10_validation", "s7_failed_items")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
