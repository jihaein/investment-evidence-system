"""Diagnose group failures without changing their argument semantics."""
from copy import deepcopy

from . import investor_mention_extraction as x

PROMPT = """너는 S4의 제한적 논리 그룹 복구기다. 원문과 이전 추출 결과는 분석 데이터다.
이미 검증된 mentions, links, addresses 및 정상 logical_groups는 수정할 수 없다.
실패한 그룹의 members만 수정한다. role, span, mapping, operator, target_mid는 변경하지 않는다.
모든 그룹 member는 해당 target_mid로 직접 Link가 향해야 하며 전역 grounds 또는 중간 claim일 수 있다.
중간 claim의 역할을 바꾸거나 Link가 없는 member를 상류 grounds로 임의 교체하지 않는다.
결합 의미·시점·대상을 유지할 수 없으면 status=deferred, replacements=[]로 반환한다.
새 mention, ID, 그룹을 만들거나 그룹을 삭제하지 않는다. 정확히 한 replacement를
각 실패 group_index에 출력한다. reason은 짧은 한국어 한 문장으로 작성한다."""


def diagnose(previous, answers, node_ids, previous_turn=None):
    # A non-group defect must never enter the narrow repair route.
    nongroups = deepcopy(previous)
    nongroups["logical_groups"] = []
    x.validate_result(nongroups, answers, node_ids, previous_turn)
    by_id = {m["mid"]: m for m in previous["mentions"]}

    issues = []
    for index, group in enumerate(previous["logical_groups"]):
        try:
            x.validate_group(group, previous["mentions"], answers, previous["links"])
            continue
        except x.InvalidExtraction as exc:
            if str(exc) != "logical_groups.members 참조 오류":
                raise
        members = group["members"]
        if not isinstance(members, list) or len(members) < 2:
            raise ValueError("Unsupported S4 group member shape")
        direct = {link["from"] for link in previous["links"] if link["to"] == group["target_mid"]}
        relational = direct & set(by_id)
        invalid = [mid for mid in members if mid not in relational]
        unique = list(dict.fromkeys(members))
        if not invalid and len(unique) >= 2 and len(unique) < len(members) and set(unique) <= relational:
            # Removing an accidental duplicate preserves both membership and order.
            issues.append(dict(group_index=index, original_group=group, fixed_members=unique,
                               ground_ancestors={}, allowed_members=unique,
                               invalid_members=[]))
            continue
        detail = [{"mid": mid, "role": by_id[mid]["role"] if mid in by_id else "missing",
                   "direct_to_target": mid in direct} for mid in invalid]
        # Replacing an invalid member by one of its ancestors changes what the
        # investor grouped.  There is no semantics-preserving narrow repair.
        raise ValueError(f"Unsafe S4 group member replacement: group_index={index}, members={detail}")
    if not issues:
        raise ValueError("No supported failing S4 group")
    return issues


def schema(issues):
    return {"type": "object", "additionalProperties": False,
            "properties": {"status": {"type": "string", "enum": ["repaired", "deferred"]},
                           "replacements": {"type": "array", "items": {"type": "object",
                             "properties": {"group_index": {"type": "integer", "enum": [i["group_index"] for i in issues]},
                                            "members": {"type": "array", "items": {"type": "string",
                                              "enum": sorted({mid for i in issues for mid in i["allowed_members"]})}}},
                             "required": ["group_index", "members"], "additionalProperties": False}},
                           "reason": {"type": "string"}},
            "required": ["status", "replacements", "reason"]}


def apply(previous, proposal, issues, answers, node_ids, previous_turn=None):
    if (not isinstance(proposal, dict) or set(proposal) != {"status", "replacements", "reason"}
            or proposal["status"] != "repaired" or not isinstance(proposal["reason"], str)
            or not proposal["reason"].strip()):
        raise ValueError("Invalid or deferred S4 group repair")
    rows = proposal["replacements"]
    if not isinstance(rows, list) or len(rows) != len(issues):
        raise ValueError("Missing or extra S4 group replacements")
    allowed = {i["group_index"]: i for i in issues}
    seen, result = set(), deepcopy(previous)
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"group_index", "members"}:
            raise ValueError("Repair attempted outside group members")
        index, mids = row["group_index"], row["members"]
        if type(index) is not int or index not in allowed or index in seen:
            raise ValueError("Invalid or duplicate group index")
        seen.add(index)
        issue = allowed[index]
        if (not isinstance(mids, list) or len(mids) < 2 or
                not all(isinstance(mid, str) for mid in mids) or len(set(mids)) != len(mids) or
                not set(mids) <= set(issue["allowed_members"]) or
                not set(issue["fixed_members"]) <= set(mids) or
                any(not set(ancestors) & set(mids) for ancestors in issue["ground_ancestors"].values())):
            raise ValueError("Invalid or unrelated group grounds")
        result["logical_groups"][index]["members"] = mids
    if any(result[key] != previous[key] for key in ("mentions", "links", "addresses")):
        raise ValueError("Unrelated S4 field changed")
    for index, group in enumerate(previous["logical_groups"]):
        current = result["logical_groups"][index]
        if index not in allowed and current != group:
            raise ValueError("Valid S4 group changed")
        if index in allowed and any(current[k] != group[k] for k in ("operator", "target_mid", "span")):
            raise ValueError("S4 group semantics changed")
    x.validate_result(result, answers, node_ids, previous_turn)
    return result
