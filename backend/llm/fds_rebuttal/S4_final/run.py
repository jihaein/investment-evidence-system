"""프로젝트 루트에서 python -B -m S4_final.run 으로 실행한다."""

import argparse
from datetime import datetime, timezone
from functools import partial
import json
import logging
import os
from contextlib import closing
from pathlib import Path
import sys
from uuid import uuid4

from . import extraction
from . import openai_api as provider


HERE = Path(__file__).resolve().parent


def save_result(folder, message, result):
    # Publish the result last so its presence identifies a complete input/output pair.
    output = folder / "investor_mentions.json"
    pending = folder / "investor_mentions.tmp"
    (folder / "investor_message.txt").write_bytes(message.encode("utf-8"))
    try:
        pending.write_bytes((json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        pending.replace(output)
    finally:
        pending.unlink(missing_ok=True)
    return output


def run_s4(investor_message, generate, *, output_dir=None, previous_turn=None, diagnostic=None, model=None):
    """Run one turn. The provider receives (instructions, input_json, schema) and returns JSON text."""
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex
    raw = None
    event = {"stage": "S4", "request_id": run_id, "attempt": 1, "model": model,
             "target": None, "use_id": None,
             "available_refs": sorted({value for r in (previous_turn or {}).get("rebuttal_items", [])
                                        for c in r.get("unverifiable", []) for value in (r["target"], c["cc_id"])})}
    folder = output_dir if output_dir is not None else HERE / "runs" / run_id
    folder.mkdir(parents=True, exist_ok=output_dir is not None)
    log = logging.Logger("s4")
    with closing(logging.FileHandler(folder / "s4_run.log", encoding="utf-8")) as handler:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
        stage = "입력 확인"
        try:
            conversation, answer_text = extraction.normalize_input(investor_message)
            stage = "new_graph.json 읽기 및 노드 사전 구성"
            graph = extraction.load_graph()
            dictionary = extraction.node_dictionary(graph)
            log.info("new_graph.json 읽음 · 노드 사전 %d개 · 엣지 %d개는 입력에서 제외",
                     len(dictionary), len(graph["edges"]))
            stage = "분석 LLM 호출"
            log.info(stage)
            if diagnostic:
                diagnostic(dict(event, outcome="started"))
            validation_error = None
            for attempt in range(2):
                instructions = extraction.INSTRUCTIONS
                if attempt:
                    instructions += (f"\n직전 출력은 검증에 실패했다: {validation_error or '참조 무결성 오류'}\n"
                                     "유효한 이전 참조 ID와 턴 번호를 다시 확인해 정확히 사용하라. "
                                     "중첩 tN 접두사, 존재하지 않는 ID, target_turn_id 불일치를 절대 출력하지 말라. "
                                     "이전 발언을 다루는 답변은 addresses를 하나 이상 출력하고, "
                                     "독립 new_evidence만 target=null, target_turn_id=null을 사용할 수 있다. "
                                     "모든 span과 connective_span은 "
                                     "질문이 아닌 개별 answer의 연속된 원문이어야 한다. "
                                     "실제 답변에 인과 연결어가 없다면 links=[]로 출력한다.")
                raw = generate(instructions, extraction.build_input(investor_message, dictionary, previous_turn),
                               extraction.OUTPUT_SCHEMA)
                stage = "응답 파싱 및 검증"
                try:
                    result = extraction.parse_response(raw)
                    extraction.validate_result(result, [t["answer"] for t in conversation] if conversation else answer_text,
                                               {node["id"] for node in dictionary}, previous_turn)
                    break
                except extraction.InvalidExtraction as exc:
                    validation_error = str(exc)
                    if attempt:
                        raise
            if diagnostic:
                diagnostic(dict(event, outcome="validated", raw_response=raw))
            for group in result.get("logical_groups", []):
                group["operator"] = extraction.grounded_operator(group)
            stage = "원문과 investor_mentions.json 저장"
            if conversation is not None:
                (folder / "conversation.json").write_text(
                    json.dumps(conversation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            output = save_result(folder, answer_text, result)
            log.info("investor_message.txt 및 investor_mentions.json 생성 · mentions=%d links=%d",
                     len(result["mentions"]), len(result["links"]))
            return output
        except Exception as exc:
            if diagnostic:
                diagnostic(dict(event, outcome="failed", raw_response=raw,
                                error=str(exc) if isinstance(exc, extraction.InvalidExtraction) else type(exc).__name__))
            # Provider payloads and arbitrary exception messages can contain sensitive data.
            detail = str(exc) if isinstance(exc, (provider.ProviderError, extraction.InvalidExtraction)) else type(exc).__name__
            log.error("%s 실패 · %s", stage, detail)
            raise RuntimeError(f"{stage} 실패 · {detail} · 로그 {folder / 's4_run.log'}") from None


def main():
    parser = argparse.ArgumentParser(description="FDS S4 투자자 발언 분석")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--message", help="투자자 원문. 생략하면 표준 입력에서 읽습니다.")
    inputs.add_argument("--input", type=Path, help="conversation 배열이 있는 요청 JSON")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", provider.DEFAULT_MODEL),
                        help="OpenAI 모델 ID. 기본값은 OPENAI_MODEL 또는 %(default)s")
    parser.add_argument("--previous-context", type=Path,
                        help="검증된 직전 턴 문맥 JSON; 첫 턴에서는 생략")
    args = parser.parse_args()
    try:
        message = (json.loads(args.input.read_text(encoding="utf-8-sig")) if args.input else
                   args.message if args.message is not None else sys.stdin.read())
        prior = json.loads(args.previous_context.read_text(encoding="utf-8-sig")) if args.previous_context else None
        print(run_s4(message, partial(provider.generate, model=args.model), previous_turn=prior))
    except (RuntimeError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
