"""Paid, opt-in acceptance run: python -m S10.live_scenarios."""
from datetime import datetime
import argparse
import json
import os
from pathlib import Path

from S10.run import ROOT, fact_ids, generate_dialogue, load_context, persona_settings

FIRST = ROOT / "phase2_runs/live_retry/793373eb3c8f4a4ba198ac17439c192a/S9/toulmin.json"
SEVENTH = ROOT / "phase2_runs/live_retry/bbd5a9dc9e1a43468768ba3fe322d413/S9/toulmin.json"


def fixture(ground, claim, *, decision="insufficient_evidence", outside=False):
    message = ground + " " + claim
    scope = [{"mid": "m1", "tag": "out_of_scope", "span": ground}] if outside else []
    return {"persona": persona_settings(), "investor_message": message, "decision": decision,
            "facts": {}, "asked_before": [], "question_candidates": [],
            "turn": {"session_id": "synthetic-s10", "turn_id": 1, "stock_code": "TEST"},
            "toulmin": {"grounds": [{"mid": "m1", "span": ground}], "claim": [{"mid": "m2", "span": claim}],
                        "warrant": {"explicit": [], "bridged": [], "chain": [], "side_assumptions": []},
                        "backing": [], "qualifier": [], "rebuttal": [], "out_of_scope": scope}}


def scenarios():
    mixed = fixture("매출은 늘었어.", "이제 영업이익도 늘 거야.", decision="mixed")
    for node, values in (("revenue", [200, 240]), ("operating_profit", [150, 96])):
        ids = []
        for value, end in zip(values, ("2025-03-31", "2025-06-30")):
            fid = f"S3-fixture-{node}-{end}"
            mixed["facts"][fid] = dict(value=value, period_end=end, unit="KRW", node_id=node,
                                       company_id="TEST", basis="OFS", measure="quarter_flow", frequency="quarterly",
                                       label={"revenue": "매출액", "operating_profit": "영업이익"}[node],
                                       series_periods=["2025-03-31", "2025-06-30"],
                                       series_direction="+" if node == "revenue" else "-")
            ids.append(fid)
        if node == "revenue":
            mixed["toulmin"]["grounds"][0]["evaluation"] = dict(tag="supported", evidence_refs=ids)
        else:
            mixed["toulmin"]["rebuttal"].append(dict(tag="undercut", target="edge:revenue->operating_profit",
                                                     reason="매출 증가에도 영업이익 감소", evidence_refs=ids))
    yield "mixed", mixed, "synthetic"
    yield "contradicted_integration", load_context(SEVENTH), str(SEVENTH.relative_to(ROOT))
    yield "leap", fixture("내 친구가 이 주식을 샀어.", "그래서 기업가치는 무조건 오를 거야.", outside=True), "synthetic"
    yield "plausible", fixture("외국인 매수가 이어진다고 들었어.", "그래서 주가가 오를 가능성이 있어.", outside=True), "synthetic"
    yield "facts_missing", load_context(FIRST), str(FIRST.relative_to(ROOT))
    cc = fixture("영업이익이 줄었어도", "일시적인 비용 탓이면 회복될 수 있어.")
    cc["toulmin"]["rebuttal"] = [dict(tag="undercut", target="edge:revenue->operating_profit",
                                      reason="이익 회복에는 비용 증가의 지속 여부 확인 필요")]
    cc["decision"] = "thesis_weakened"
    cc["asked_before"] = ["cc_orders"]
    cc["question_candidates"] = [dict(cc_id="cc_orders", text="주문이 늘었는지"),
                                 dict(cc_id="cc_cost", text="비용 증가가 일회성이었는지")]
    yield "asked_before", cc, "synthetic"
    yield "json", load_context(FIRST), str(FIRST.relative_to(ROOT))


def write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="+", help="Select named cases for targeted verification")
    selected = parser.parse_args().only
    folder = ROOT / "S10/runs" / datetime.now().strftime("review_%Y%m%d_%H%M%S")
    model = os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
    print(folder, model, flush=True)
    failures = []
    for name, context, source in scenarios():
        if selected and name not in selected:
            continue
        target = folder / name
        target.mkdir(parents=True)
        for row in context["toulmin"]["claim"] + context["toulmin"]["grounds"]:
            if row.get("source_turn_id", context["turn"]["turn_id"]) == context["turn"]["turn_id"]:
                if row["span"] not in context["investor_message"]:
                    raise ValueError("scenario span absent from current speech")
        if set(fact_ids(context["toulmin"])) != set(context["facts"]):
            raise ValueError("scenario fact mismatch")
        audit = {}
        try:
            dialogue, status = generate_dialogue(context, model=model, audit=audit)
            payload = audit.pop("request")
            # These seven cases contain authored example conversations only.
            write(target / "request.json", payload)
            write(target / "dialogue.json", dialogue)
            checks = {"generated_without_fallback": not status["fallback"]}
            if name in ("plausible", "leap"):
                checks["scope_action"] = status.get("scope_case") == name
            if name in ("facts_missing", "json"):
                checks["uncorroborated_product_outlook"] = status.get("scope_case") == "plausible"
            if name == "asked_before":
                checks["filtered_history"] = [q["cc_id"] for q in payload["question_candidates"]] == ["cc_cost"]
                checks["asked_condition"] = dialogue["asked_cc_id"] == "cc_cost"
            if name == "contradicted_integration":
                checks["actual_fact_citation"] = bool(status.get("citations"))
            write(target / "validation.json", dict(model=model, source=source, checks=checks, **status, **audit))
            if not all(checks.values()):
                failures.append(name)
            print(name, json.dumps([dialogue, status], ensure_ascii=False), flush=True)
        except Exception as exc:
            failures.append(name)
            write(target / "error.json", {"type": type(exc).__name__, "reason": str(exc)})
    if failures:
        raise SystemExit(f"failed scenarios: {failures}")


if __name__ == "__main__":
    main()
