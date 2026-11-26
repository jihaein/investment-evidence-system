"""Validate S4 evidence, retaining unresolved graph mappings for inspection."""
from collections import defaultdict

from S4_investor_extraction.investor_mention_extraction import CHOICES, InvalidExtraction, grounded_operator, is_excerpt, validate_group
from .graph_loader import indexed, require


def map_mentions(out, s4, message, graph):
    require(isinstance(message, str) and message.strip(), "empty investor_message")
    require(isinstance(s4, dict), "S4 must be an object")
    require(all(isinstance(s4.get(k), list) for k in ("mentions", "links", "addresses")), "invalid S4 arrays")
    mentions = indexed(s4["mentions"], "mid")
    for mid, m in mentions.items():
        original_message = m.get("source_message", message)
        require(is_excerpt(m["span"], original_message) and isinstance(m["qualifiers"], list)
                and all(is_excerpt(q, original_message) for q in m["qualifiers"]), f"{mid}: invalid original span")
        for k, values in CHOICES.items():
            require(m[k] in values or (k == "direction" and m[k] is None), f"{mid}: invalid {k}")
        primary, alts = m["node_id"], m["alt_node_ids"]
        require((primary is None or isinstance(primary, str)) and isinstance(alts, list)
                and all(isinstance(a, str) for a in alts), f"{mid}: invalid mapping fields")
        ids = list(dict.fromkeys(n for n in [primary, *alts] if n is not None))
        valid = [n for n in ids if n in graph.nodes]
        invalid = [n for n in ids if n not in graph.nodes]
        status = "unmapped" if not valid else "mapped" if primary in valid and len(ids) == 1 else "ambiguous"
        mapping = {"mid": mid, "node_id": primary, "alt_node_ids": alts, "status": status,
                   "valid_candidates": valid, "invalid_ids": invalid}
        out["mappings"][mid] = mapping
        if invalid:
            out["unresolved"].append({"reason": "unknown_node_ids", "mid": mid, "node_ids": invalid})
        if status != "mapped":
            out["unresolved"].append({"reason": status + "_mention", "mid": mid})
        if status == "unmapped":
            out["unmapped"].append(m)
        if primary in graph.nodes:
            use = dict(m, use_id="mention:" + mid, origin="explicit", source="s4_mention",
                       tentative=status != "mapped")
            out["nodes"].setdefault(primary, {"uses": []})["uses"].append(use)
        if m["role"] == "claim":
            out["claims"].append(dict(mapping))
            if status != "mapped":
                out["unresolved"].append({"reason": "unresolved_claim", "mid": mid})
        else:
            out["grounds"].append(mid)
    if not out["claims"]:
        out["unresolved"].append({"reason": "missing_claim"})
    elif len(out["claims"]) == 1 and out["claims"][0]["status"] == "mapped":
        out["claim_node"] = out["claims"][0]["node_id"]
    for i, link in enumerate(s4["links"], 1):
        a, b = link["from"], link["to"]
        require(isinstance(a, str) and isinstance(b, str)
                and is_excerpt(link["connective_span"], link.get("source_message", message)), f"invalid link {i}")
        lid = f"L{i}"
        out["explicit_links"][lid] = dict(link, link_id=lid, source="s4_link", origin="explicit",
                                         path_ids=[], status="pending")
        if a not in mentions or b not in mentions or a == b:
            out["explicit_links"][lid]["status"] = "invalid_reference"
            out["unresolved"].append({"reason": "invalid_link_reference", "link_id": lid})
    return mentions


def logical_groups(out, s4, message):
    groups = s4.get("logical_groups", [])
    require(isinstance(groups, list), "invalid logical_groups")
    result = out["logical_groups"]
    for i, group in enumerate(groups):
        if group.get("source_turn_id") is None:
            try:
                validate_group(group, s4["mentions"], message, s4["links"])
            except InvalidExtraction as exc:
                out["unresolved"].append({"reason": "invalid_logical_group", "s4_index": i, "detail": str(exc)})
                continue
        else:
            require(group["target_mid"] in {m["mid"] for m in s4["mentions"]}
                    and set(group["members"]) <= {m["mid"] for m in s4["mentions"]},
                    "inherited logical group references missing")
        g = dict(group, group_id=f"G{i+1}", source="s4_group", s4_index=i)
        operator = g["operator"]
        if grounded_operator(g) != operator:
            g.update(reported_operator=operator, operator="UNSPECIFIED")
            out["unresolved"].append({"reason": "unclear_group_cue", "group_id": g["group_id"]})
        result.append(g)
    by_scope = defaultdict(list)
    for g in result:
        by_scope[g["target_mid"], frozenset(g["members"])].append(g)
    for scoped in by_scope.values():
        if {g["operator"] for g in scoped} >= {"AND", "OR"}:
            for g in scoped:
                g.update(reported_operator=g.get("reported_operator", g["operator"]), operator="UNSPECIFIED")
            out["unresolved"].append({"reason": "conflicting_logical_groups",
                                      "group_ids": [g["group_id"] for g in scoped]})
    incoming = defaultdict(list)
    for link in out["explicit_links"].values():
        a, b = link["from"], link["to"]
        if link["status"] != "invalid_reference" and a in out["grounds"] and a not in incoming[b]:
            incoming[b].append(a)
    for target, mids in incoming.items():
        if len(mids) >= 2 and (target, frozenset(mids)) not in by_scope:
            result.append({"group_id": f"U{len(result)+1}", "members": mids, "target_mid": target,
                           "operator": "UNSPECIFIED", "span": None, "source": "common_s4_link_target"})
