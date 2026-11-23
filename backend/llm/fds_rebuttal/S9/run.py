"""Assemble one S8 assessment into Toulmin's six parts; no inference or API calls."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
from runtime_inputs import card_bundle, cached_json, file_sha256

ROOT = Path(__file__).resolve().parents[1]
_READ_CACHE = {}


def read(path):
    path = Path(path).resolve()
    stat = path.stat()
    signature = (stat.st_mtime_ns, stat.st_size)
    entry = _READ_CACHE.get(path)
    if entry is None or entry[0] != signature:
        entry = signature, json.loads(path.read_text(encoding="utf-8-sig"))
        _READ_CACHE[path] = entry
    return entry[1]


def source_path(value):
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def require(ok, message):
    if not ok:
        raise ValueError(message)


def assemble(assessment_path):
    assessment_path = Path(assessment_path).resolve()
    a = read(assessment_path)
    require(a.get("schema_version") == "assessment.1" and a.get("summary", {}).get("decided_by_rule"),
            "S8 최종 assessment.json이 필요합니다")
    src = a["sources"]
    argument_path = source_path(src["investor_argument"])
    argument = read(argument_path)
    require(file_sha256(argument_path) == src["investor_sha256"],
            "S5 원본 해시 불일치")
    require((a["session_id"], a["turn_id"], a["graph_version"], a["graph_schema_sha256"]) ==
            (argument["session_id"], argument["turn_id"], argument["graph_version"],
             argument["graph_schema_sha256"]), "S5/S8 턴·그래프 불일치")
    require(a["claims"] == argument["claims"] and a["grounds"] == argument["grounds"] and
            a["logical_groups"] == argument["logical_groups"], "S5/S8 논증 참조 불일치")
    company_file = cached_json(source_path(src["company_argument"]))
    require((company_file["graph_version"], company_file["as_of"]) ==
            (a["graph_version"], a["as_of"]), "S3 그래프·기준일 불일치")
    company = company_file["companies"][a["stock_code"]]
    facts = cached_json(source_path(src["facts"]))["facts"]
    cards, _ = card_bundle(source_path(src["edge_cards"]))
    mentions = {m["mid"]: m for m in argument["investor_mentions"]["mentions"]}
    tags = {(t["target"], t["use_id"]): t for t in a["item_tags"]}
    require(len(tags) == len(a["item_tags"]), "확정 태그 사용 ID 중복")

    def refs(row):
        return {k: row.get(k) for k in ("target", "use_id", "target_origin", "tag", "relation",
                "reason", "evidence_refs", "mid", "path_id", "link_id", "side_id", "tagged_by",
                "rule_id", "review_required", "review_reason") if row.get(k) is not None}

    def mention(mid):
        m = mentions.get(mid)
        require(m is not None, f"S5 원문 Mention 없음: {mid}")
        node_id = a["mappings"].get(mid, {}).get("node_id")
        row = tags.get((f"node:{node_id}", f"mention:{mid}")) if node_id else None
        result = {k: m.get(k) for k in ("mid", "node_id", "span", "direction", "horizon",
                  "certainty", "qualifiers", "alt_node_ids")}
        if "source_turn_id" in m:
            result["source_turn_id"] = m["source_turn_id"]
            result["source_mid"] = m.get("source_mid", mid)
        result["mapping_status"] = a["mappings"].get(mid, {}).get("status")
        result["evaluation"] = refs(row) if row else None
        return result

    claim = [mention(x["mid"]) for x in a["claims"]]
    grounds = [mention(mid) for mid in a["grounds"]]
    warrant = {"explicit": [], "bridged": [], "chain": [], "side_assumptions": []}
    for path in argument["paths"].values():
        origin = path["origin"]
        require(origin in ("explicit", "bridged", "chain"), f"경로 유형 오류: {origin}")
        item = {k: path.get(k) for k in ("path_id", "origin", "link_id", "from_mid", "to_mid",
                "nodes", "edges", "horizon", "tentative", "expected_directions")}
        item["edge_uses"] = [refs(t) for t in a["item_tags"] if t.get("path_id") == path["path_id"]
                             and t["target"].startswith("edge:")]
        warrant[origin].append(item)
    for side in argument["side_checks"].values():
        item = {k: side.get(k) for k in ("side_id", "path_id", "node_id", "target_node", "required",
                "via_edge", "from_mid", "to_mid", "horizon", "tentative")}
        item["evaluation"] = refs(tags[(f"node:{side['node_id']}", side["side_id"])]) if (
            f"node:{side['node_id']}", side["side_id"]) in tags else None
        warrant["side_assumptions"].append(item)

    def cc_details(row, key):
        if not row["target"].startswith("edge:"):
            return []
        eid = row["target"][5:]
        card = cards.get(eid)
        require(card is not None, f"S2 카드 없음: {eid}")
        lookup = {cc["cc_id"]: cc["text"] for cc in card["counterconditions"]}
        result = []
        for ccid in row.get(key, []):
            require(ccid in lookup, f"S2 CC 없음: {eid}/{ccid}")
            result.append({"cc_id": ccid, "edge_id": eid, "text": lookup[ccid]})
        return result

    backing, rebuttal = [], []
    for row in a["item_tags"]:
        for evidence in row.get("evidence_refs", []):
            if evidence.startswith("S3-"):
                require(evidence in facts and facts[evidence]["company_id"] == a["stock_code"],
                        f"기업 fact 참조 오류: {evidence}")
        edge = row["target"].startswith("edge:")
        if edge and (row["tag"] == "supported" or row["tag"] == "direction_only"
                     and row.get("relation") == "consistent"):
            eid = row["target"][5:]
            require(eid in cards, f"Backing S2 카드 없음: {eid}")
            backing.append(dict(refs(row), edge_id=eid, mechanism=cards[eid]["mechanism"],
                                card_source=cards[eid]["source"], review_status=cards[eid]["review_status"]))
        if row["tag"] in ("contradicted", "undercut") or row["tag"] == "direction_only" and row.get("relation") in ("against", "context_dependent"):
            rebuttal.append(dict(refs(row), triggered=cc_details(row, "triggered"),
                                 unverifiable=cc_details(row, "unverifiable")))

    qualifier = []
    for c in claim:
        mid = c["mid"]
        related = [u for u in a["unresolved_items"] if u.get("mid") == mid or
                   u.get("target") == f"node:{c['node_id']}"]
        row = tags.get((f"node:{c['node_id']}", f"mention:{mid}"))
        qualifier.append({"mid": mid, "certainty": c["certainty"], "horizon": c["horizon"],
                          "qualifiers": c["qualifiers"], "company_status": row.get("company_status") if row else None,
                          "unresolved": related})
    cc_index = {}
    for row in a["item_tags"]:
        if row["target"].startswith("edge:"):
            for detail in cc_details(row, "unverifiable"):
                ccid = detail["cc_id"]
                require(ccid not in cc_index or cc_index[ccid] == detail,
                        f"CC ID가 여러 엣지에서 충돌: {ccid}")
                cc_index[ccid] = detail
    candidate_ids = a["summary"]["question_candidates"]
    require(len(candidate_ids) == len(set(candidate_ids)) and set(candidate_ids) <= set(cc_index),
            "S8 질문 후보의 S2 카드 참조 오류")
    result = {k: a[k] for k in ("session_id", "turn_id", "stock_code", "period", "as_of", "graph_version")}
    reply = a.get("reply_context")
    if reply is not None:
        reply = dict(reply, current_addresses=argument["investor_mentions"].get("addresses", []),
                     cc_evaluations=[row for row in a["item_tags"] if row["target"].startswith("cc:")])
    result.update({"schema_version": "toulmin.1", "graph_schema_sha256": a["graph_schema_sha256"],
                   "decision": a["summary"]["decision"], "decided_by_rule": a["summary"]["decided_by_rule"],
                   "claim": claim, "grounds": grounds, "warrant": warrant, "backing": backing,
                   "qualifier": qualifier, "rebuttal": rebuttal,
                   "out_of_scope": [refs(t) for t in a["item_tags"] if t["tag"] == "out_of_scope"],
                   "logical_groups": a["logical_groups"], "based_on_turn": a.get("based_on_turn"),
                   "reply_context": reply, "active_lineage": a.get("active_lineage"),
                   "question_candidates": [cc_index[x] for x in candidate_ids],
                   "question_history": a["summary"]["question_history"],
                   "sources": dict(src, assessment=str(assessment_path),
                                   assessment_sha256=file_sha256(assessment_path)),
                   "unresolved_items": a["unresolved_items"]})
    return result


def main():
    parser = argparse.ArgumentParser(description="S9 Toulmin assembly from final S8 assessment")
    parser.add_argument("--assessment", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or ROOT / "S9" / "runs" / args.assessment.parent.parent.name / "toulmin.json"
    try:
        result = assemble(args.assessment)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ValueError, KeyError, OSError, TypeError) as exc:
        parser.exit(1, f"S9 failed: {exc}\n")
    print(output)


if __name__ == "__main__":
    main()
