"""Run from the project root with python -B -m S5.run."""
import argparse
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from .argument import build
from .graph import Graph, read_json, require
from .validation import verify, verify_company_join
from .replies import attach_lineage, effective_s4, compose_active
from S4_final.extraction import validate_result
from hashlib import sha256

HERE = Path(__file__).resolve().parent
GRAPH = HERE.parent / "S1.인과그래프" / "new_graph.json"


def run(mentions_path, session_id, turn_id, output, *, graph_path=GRAPH, config_path=HERE/"config.json",
        message_path=None, company_path=None, generate=None, previous_argument_path=None,
        previous_turn=None):
    message_path = message_path or mentions_path.with_name("investor_message.txt")
    inputs = [mentions_path, message_path, graph_path, config_path]
    if company_path:
        inputs.append(company_path)
    if previous_argument_path:
        inputs.append(previous_argument_path)
    require(output.resolve() not in {p.resolve() for p in inputs}, "output collides with an input")
    graph, s4 = Graph(read_json(graph_path)), read_json(mentions_path)
    message = message_path.read_bytes().decode("utf-8-sig")
    require((turn_id == 1 and previous_argument_path is None and previous_turn is None) or
            (turn_id > 1 and previous_argument_path is not None and previous_turn is not None),
            "previous turn required for reply")
    validate_result(s4, message, set(graph.nodes), previous_turn)
    effective, suppressed = effective_s4(s4) if previous_argument_path else (s4, [])
    prior = read_json(previous_argument_path) if previous_argument_path else None
    if prior:
        verify(prior, graph, prior["investor_mentions"])
        effective = compose_active(prior, effective, s4["addresses"], turn_id)
    out = build(effective, message, graph, read_json(config_path), session_id, turn_id, generate=generate)
    if previous_argument_path:
        attach_lineage(out, prior, sha256(previous_argument_path.read_bytes()).hexdigest(),
                       previous_argument_path.resolve(), s4, suppressed)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                     prefix=".investor-", suffix=".tmp", delete=False) as f:
        pending = Path(f.name)
    try:
        pending.write_text(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")
        saved = read_json(pending)
        verify(saved, graph, effective)
        if company_path:
            verify_company_join(saved, read_json(company_path))
        os.replace(pending, output)
    finally:
        pending.unlink(missing_ok=True)
    return out


def main():
    parser = argparse.ArgumentParser(description="S4 → S5 investor_argument.json")
    parser.add_argument("--mentions", required=True, type=Path)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--turn-id", required=True, type=int)
    parser.add_argument("--message-file", type=Path)
    parser.add_argument("--graph", type=Path, default=GRAPH)
    parser.add_argument("--config", type=Path, default=HERE/"config.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--company-argument", type=Path, help="Optional read-only S3 graph/key check")
    parser.add_argument("--previous-argument", type=Path, help="Prior immutable S5 argument for a reply")
    parser.add_argument("--previous-context", type=Path, help="Validated previous_turn JSON for a reply")
    args = parser.parse_args()
    output = args.output or HERE/"runs"/uuid4().hex/"investor_argument.json"
    try:
        out = run(args.mentions, args.session_id, args.turn_id, output, graph_path=args.graph,
                  config_path=args.config, message_path=args.message_file, company_path=args.company_argument,
                  previous_argument_path=args.previous_argument,
                  previous_turn=read_json(args.previous_context) if args.previous_context else None)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, f"S5 validation failed: {exc}\n")
    print(f"{output.resolve()} | paths={len(out['paths'])} side={len(out['side_checks'])} "
          f"unresolved={len(out['unresolved'])}")


if __name__ == "__main__":
    main()
