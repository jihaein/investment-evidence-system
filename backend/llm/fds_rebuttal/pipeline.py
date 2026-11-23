"""Run one investor turn or resume the first unpublished stage of that run."""
import argparse
from contextlib import closing
from functools import partial
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from time import perf_counter
from uuid import uuid4

from S4_investor_extraction import openai_api_client as provider
from S4_investor_extraction.extract_investor_mentions import run_s4
from S4_investor_extraction.investor_mention_extraction import normalize_input
from runtime_inputs import card_bundle, company_id, file_sha256, select_company_argument
from S5_investor_arguments.run_investor_arguments import run as run_s5
from S6_S7_S8_tagging_validation.tagging_and_assessment_pipeline import PERIODS, ROOT, run_s6, run_s7, run_s8, run_assessment

RUNS = ROOT / "pipeline_runs"


def previous_run(runs, session, turn, code, period):
    if turn == 1:
        return None
    matches = []
    for request in runs.glob("*/request.sqlite"):
        with closing(sqlite3.connect(request)) as db:
            row = db.execute("SELECT session, turn, stock_code, period FROM request").fetchone()
        if row and row[:2] == (session, turn - 1):
            if row[2:] != (code, period):
                raise ValueError("이전 턴의 기업·기간이 현재 턴과 다릅니다. 새 기간의 근거는 별도 평가가 필요합니다.")
            matches.append(request.parent)
    if len(matches) != 1:
        raise ValueError("직전 완료 턴을 유일하게 찾을 수 없습니다")
    previous = matches[0]
    for rel in ("S5/investor_argument.json", "S6/tagging_draft.json", "S8/assessment.json"):
        if not (previous / rel).is_file():
            raise ValueError(f"이전 턴의 {rel}이 없습니다")
    return previous


def previous_context(previous, session, turn, code, period, question_cc=None, company_data=None):
    if previous is None:
        return None
    argument_path = previous / "S5/investor_argument.json"
    draft_path = previous / "S6/tagging_draft.json"
    assessment_path = previous / "S8/assessment.json"
    argument = json.loads(argument_path.read_text(encoding="utf-8-sig"))
    draft = json.loads(draft_path.read_text(encoding="utf-8-sig"))
    assessment = json.loads(assessment_path.read_text(encoding="utf-8-sig"))
    company_path = PERIODS / f"company.argument{period}.json"
    s3 = company_data if company_data is not None else json.loads(company_path.read_text(encoding="utf-8-sig"))
    if (argument["session_id"], argument["turn_id"], draft["session_id"], draft["turn_id"],
        draft["stock_code"], draft["period"], draft["as_of"]) != (session, turn - 1, session, turn - 1,
                                                                  code, period, s3["as_of"]):
        raise ValueError("이전 턴 식별자·기업·기준일 불일치")
    if argument["graph_version"] != s3["graph_version"] or argument["graph_schema_sha256"] != s3["s3_metadata"]["graph_schema_sha256"]:
        raise ValueError("이전/현재 그래프 버전 불일치")
    if file_sha256(argument_path) != draft["sources"]["investor_sha256"]:
        raise ValueError("이전 S5 원본 해시 불일치")
    if (assessment["session_id"], assessment["turn_id"], assessment["stock_code"],
        assessment["period"], assessment["graph_version"], assessment["graph_schema_sha256"]) != (
            session, turn - 1, code, period, argument["graph_version"], argument["graph_schema_sha256"]):
        raise ValueError("이전 최종 S8 평가 식별자 불일치")
    if assessment["inputs"]["tagging_draft_sha256"] != file_sha256(draft_path):
        raise ValueError("이전 최종 S8 평가의 S6 해시 불일치")
    validated_path = previous / "S8/tagging_validated.json"
    if (file_sha256(validated_path) !=
            assessment["inputs"]["tagging_validated_sha256"] or
            json.loads(validated_path.read_text(encoding="utf-8-sig"))["item_tags"] != assessment["item_tags"]):
        raise ValueError("이전 최종 S8 평가의 검증 태그 불일치")
    company = s3["companies"][code]
    rebuttals = []
    cards, _ = card_bundle(ROOT / "S2_edge_cards/edge_cards")
    for row in assessment["item_tags"]:
        if row.get("tag") != "undercut" or not row.get("target", "").startswith("edge:"):
            continue
        eid = row["target"][5:]
        if (row.get("tentative") or eid not in argument["edges"]
                or row.get("use_id") not in {u["use_id"] for u in argument["edges"][eid]["uses"]}
                or row["unverifiable"] != company["edges"][eid]["unverifiable"]):
            raise ValueError("이전 undercut 엣지와 S3 관측 불일치")
        card = cards.get(eid)
        known = {c["cc_id"]: c["text"] for c in card["counterconditions"]} if card else {}
        uv = [dict(cc_id=cc, text=known[cc]) for cc in row["unverifiable"] if cc in known]
        rebuttals.append(dict(target=row["target"], tag="undercut", use_id=row["use_id"],
                              unverifiable=uv, source_turn_id=turn - 1,
                              context={"reason": row.get("reason"),
                                       "comparison": company["edges"][eid].get("comparison"),
                                       "as_of": s3["as_of"], "period": period}))
    if question_cc is not None and question_cc not in {c["cc_id"] for r in rebuttals for c in r["unverifiable"]}:
        raise ValueError("이전 질문 cc_id가 이전 undercut 조건에 없습니다")
    snapshots = [*argument.get("lineage", []),
                 {"source_turn_id": argument["turn_id"], "argument": argument,
                  "active": argument.get("current_active") or __import__("S5.replies", fromlist=["active_ids"]).active_ids(argument)}]
    targets, nodes = [], set()
    for item in snapshots:
        tid, src, active = item["source_turn_id"], item["argument"], item["active"]
        targets.extend(f"t{tid}:mention:{x}" for x in active["mentions"])
        targets.extend(f"t{tid}:link:{x}" for x in active["links"])
        for mid in active["mentions"]:
            m = next(x for x in src["investor_mentions"]["mentions"] if x["mid"] == mid)
            if m["node_id"]:
                nodes.add(m["node_id"])
                targets.append(f"t{tid}:node:{m['node_id']}")
        for pid in active["paths"]:
            targets.extend(f"t{tid}:edge:{eid}" for eid in src["paths"][pid]["edges"])
    return {"turn_id": turn - 1, "nodes": sorted(nodes), "rebuttal_items": rebuttals,
            "last_question_cc_id": question_cc, "reference_targets": sorted(set(targets)),
            "source_turn_ids": sorted({s["source_turn_id"] for s in snapshots}),
            "argument_sha256": file_sha256(argument_path),
            "draft_sha256": file_sha256(draft_path),
            "assessment_path": str(assessment_path.resolve()),
            "assessment_sha256": file_sha256(assessment_path),
            "stock_code": code, "period": period, "as_of": s3["as_of"],
            "graph_version": s3["graph_version"],
            "graph_schema_sha256": s3["s3_metadata"]["graph_schema_sha256"],
            "cards_sha256": s3["s3_metadata"]["cards_sha256"]}


def create_run(message, session, turn, stock_code=None, period=None, *, runs=RUNS, last_question_cc_id=None, alert_date=None):
    conversation, answer_text = normalize_input(message)
    if not session.strip() or turn < 1:
        raise ValueError("세션 ID와 양의 턴 번호가 필요합니다")
    if isinstance(message, dict):
        code = company_id(message)
        if stock_code is not None and stock_code != code:
            raise ValueError("request company mismatch")
        stock_code = code
        if alert_date is not None and alert_date != message.get("alert_date"):
            raise ValueError("request alert_date mismatch")
        alert_date = message.get("alert_date")
    if alert_date is None:
        raise ValueError("alert_date is required; period alone cannot establish availability")
    selected, selected_data = select_company_argument(stock_code, alert_date, PERIODS)
    selected_period = selected.stem.removeprefix("company.argument")
    if period is not None and period != selected_period:
        raise ValueError("period does not match alert_date")
    period = selected_period
    for request in runs.glob("*/request.sqlite"):
        with closing(sqlite3.connect(request)) as db:
            row = db.execute("SELECT session, turn FROM request").fetchone()
        if row == (session, turn):
            raise ValueError("같은 세션·턴의 실행이 이미 존재합니다")
    prior = previous_run(runs, session, turn, stock_code, period)
    if turn == 1 and last_question_cc_id is not None:
        raise ValueError("첫 턴에는 이전 질문 CC가 없습니다")
    if prior:
        previous_context(prior, session, turn, stock_code, period, last_question_cc_id,
                         company_data=selected_data)
    job = runs / uuid4().hex
    job.mkdir(parents=True)
    with closing(sqlite3.connect(job / "request.sqlite")) as db, db:
        db.execute("CREATE TABLE request(message, session, turn INTEGER, stock_code TEXT, period, model, previous_run_id, last_question_cc_id, alert_date, conversation_json)")
        db.execute("INSERT INTO request VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (answer_text, session, turn, stock_code, period, os.getenv("OPENAI_MODEL") or provider.DEFAULT_MODEL,
                    prior.name if prior else None, last_question_cc_id, alert_date,
                    json.dumps(conversation, ensure_ascii=False) if conversation is not None else None))
    return job


def execute(job, *, extract=None, select=None, diagnostic=None):
    if not (job / "request.sqlite").is_file():
        raise ValueError(f"실행 기록을 찾을 수 없습니다: {job}")
    # Each job has its own OS-backed SQLite lock, released even on process exit.
    with closing(sqlite3.connect(job / "request.sqlite", timeout=0)) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                raise ValueError("같은 실행 ID가 이미 처리 중입니다.") from exc
            raise
        request = db.execute("SELECT * FROM request").fetchone()
        message, session, turn, code, period, model = request[:6]
        if len(request) < 10 or not request[8]:
            raise ValueError("legacy run has no alert_date; archived runs cannot be resumed")
        selected, selected_data = select_company_argument(code, request[8], PERIODS)
        if selected.stem != "company.argument" + period:
            raise ValueError("stored period does not match alert_date")
        if request[9]:
            message = json.loads(request[9])
        prior = job.parent / request[6] if len(request) > 6 and request[6] else None
        previous = previous_context(prior, session, turn, code, period,
                                    request[7] if len(request) > 7 else None,
                                    company_data=selected_data)
        mentions = job / "S4/investor_mentions.json"
        argument = job / "S5/investor_argument.json"
        draft = job / "S6/tagging_draft.json"
        llm = job / "S7/tagging_llm.json"
        validated = job / "S8/tagging_validated.json"
        assessment = job / "S8/assessment.json"
        s6_context = None

        def mechanical():
            nonlocal s6_context
            _, s6_context = run_s6(argument, code, period, draft.parent,
                                   previous_draft_path=prior / "S6/tagging_draft.json" if prior else None,
                                   previous_context=previous, alert_date=request[8],
                                   company_input=(selected, selected_data))

        stages = [
            ("S4", mentions, lambda: run_s4(message, extract or partial(provider.generate, model=model),
                                             output_dir=mentions.parent, previous_turn=previous,
                                             diagnostic=diagnostic, model=model)),
            ("S5", argument, lambda: run_s5(mentions, session, turn, argument, generate=select,
                                             previous_argument_path=prior / "S5/investor_argument.json" if prior else None,
                                             previous_turn=previous)),
            ("S6", draft, mechanical),
            ("S7", llm, lambda: run_s7(draft, llm.parent, context=s6_context, model=model, diagnostic=diagnostic)),
            ("S8", validated, lambda: run_s8(draft, llm, validated.parent, context=s6_context)),
            ("S8-aggregation", assessment, lambda: run_assessment(validated, assessment.parent)),
        ]
        for stage, output, action in stages:
            if output.is_file():
                print(f"{stage} 기존 결과 사용", file=sys.stderr)
                continue
            start = perf_counter()
            try:
                action()
            except Exception as exc:
                raise RuntimeError(f"{stage} 실패: {exc}") from exc
            print(f"{stage} 완료 ({perf_counter()-start:.2f}s)", file=sys.stderr)
        return assessment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_subparsers(dest="action", required=True)
    start = group.add_parser("start")
    inputs = start.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--message")
    inputs.add_argument("--input", type=Path)
    start.add_argument("--alert-date")
    start.add_argument("--session-id", required=True)
    start.add_argument("--turn-id", required=True, type=int)
    start.add_argument("--stock-code")
    start.add_argument("--period")
    start.add_argument("--last-question-cc-id", help="이전 턴 질문의 CC ID (있는 경우)")
    group.add_parser("resume").add_argument("run_id")
    args = parser.parse_args()
    try:
        if args.action == "start":
            message = json.loads(args.input.read_text(encoding="utf-8-sig")) if args.input else args.message
            job = create_run(message, args.session_id, args.turn_id, args.stock_code, args.period,
                             last_question_cc_id=args.last_question_cc_id, alert_date=args.alert_date)
        else:
            if not re.fullmatch(r"[0-9a-f]{32}", args.run_id):
                raise ValueError("잘못된 실행 ID")
            job = RUNS / args.run_id
        print(f"run_id={job.name}", file=sys.stderr, flush=True)
        print(execute(job))
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
