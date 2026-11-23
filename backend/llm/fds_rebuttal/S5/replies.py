"""Turn-scoped immutable argument snapshots; selected S5 paths remain untouched."""
from copy import deepcopy
import re

from .graph import require


def effective_s4(s4):
    """Do not turn a withdrawal, repetition, or CC explanation into new evidence."""
    result = deepcopy(s4)
    spans = [a["span"] for a in s4["addresses"] if a["type"] != "new_evidence"]
    removed = {m["mid"] for m in s4["mentions"] if any(m["span"] in span for span in spans)}
    result["mentions"] = [m for m in result["mentions"] if m["mid"] not in removed]
    result["links"] = [l for l in result["links"] if l["from"] not in removed and l["to"] not in removed]
    result["logical_groups"] = [g for g in result.get("logical_groups", [])
                                if g["target_mid"] not in removed and not set(g["members"]) & removed]
    return result, sorted(removed)


def compose_active(prior, current, addresses, turn_id):
    """Rebuild the current S4-shaped argument from active prior speech and new speech."""
    active = prior.get("current_active") or active_ids(prior)
    old = {m["mid"]: m for m in prior["investor_mentions"]["mentions"]}
    retired_mids, retired_links = set(), set()
    for address in addresses:
        if address["type"] != "retraction":
            continue
        match = re.fullmatch(r"t(\d+):(mention|link|node|edge):(.+)", address["target"])
        require(match, "invalid retraction reference")
        source_turn, kind, value = int(match[1]), match[2], match[3]
        for mid in active["mentions"]:
            m = old[mid]
            if m.get("source_turn_id", prior["turn_id"]) != source_turn:
                continue
            source_mid = m.get("source_mid", mid)
            if kind == "mention" and source_mid == value or kind == "node" and m["node_id"] == value:
                retired_mids.add(mid)
        for lid in active["links"]:
            link = prior["explicit_links"][lid]
            if link.get("source_turn_id", prior["turn_id"]) == source_turn and (
                    kind == "link" and link.get("source_link_id", lid) == value or
                    kind == "edge" and any(value in prior["paths"][pid]["edges"]
                                           for pid in link["path_ids"] if pid in active["paths"])):
                retired_links.add(lid)
    ids = {}
    inherited = []
    for mid in active["mentions"]:
        if mid in retired_mids:
            continue
        m = deepcopy(old[mid])
        src = m.get("source_turn_id", prior["turn_id"])
        original = m.get("source_mid", mid)
        new_id = f"t{src}:{original}"
        ids[mid] = new_id
        m.update(mid=new_id, source_turn_id=src, source_mid=original,
                 source_message=m.get("source_message", prior["investor_message"]))
        inherited.append(m)
    links = []
    for lid in active["links"]:
        link = prior["explicit_links"][lid]
        if lid in retired_links or link["from"] not in ids or link["to"] not in ids:
            continue
        links.append({"from": ids[link["from"]], "to": ids[link["to"]],
                      "connective_span": link["connective_span"],
                      "source_turn_id": link.get("source_turn_id", prior["turn_id"]),
                      "source_link_id": link.get("source_link_id", lid),
                      "source_message": link.get("source_message", prior["investor_message"])})
    inherited_groups = []
    for group in prior["logical_groups"]:
        if group["target_mid"] in ids and set(group["members"]) <= ids.keys():
            inherited_groups.append({"members": [ids[mid] for mid in group["members"]],
                                     "target_mid": ids[group["target_mid"]],
                                     "operator": group["operator"], "span": group.get("span"),
                                     "source_turn_id": group.get("source_turn_id", prior["turn_id"])})
    fresh = deepcopy(current)
    for m in fresh["mentions"]:
        m.update(source_turn_id=turn_id, source_mid=m["mid"])
    for i, link in enumerate(fresh["links"], 1):
        link.update(source_turn_id=turn_id, source_link_id=f"L{i}")
    require(not {m["mid"] for m in inherited} & {m["mid"] for m in fresh["mentions"]},
            "inherited/current Mention ID collision")
    return {"mentions": inherited + fresh["mentions"], "links": links + fresh["links"],
            "addresses": fresh["addresses"],
            "logical_groups": inherited_groups + fresh.get("logical_groups", [])}


def active_ids(argument):
    return {
        "mentions": [m["mid"] for m in argument["investor_mentions"]["mentions"]],
        "links": list(argument["explicit_links"]), "paths": list(argument["paths"]),
        "sides": list(argument["side_checks"]),
        "node_uses": [u["use_id"] for e in argument["nodes"].values() for u in e["uses"]],
        "edge_uses": [u["use_id"] for e in argument["edges"].values() for u in e["uses"]],
        "connections": list(range(len(argument["ground_claim_connections"]))),
        "groups": [g["group_id"] for g in argument["logical_groups"]],
    }


def retire(entry, target):
    """Remove dependent active references, without editing the saved argument."""
    arg, active = entry["argument"], entry["active"]
    match = re.fullmatch(r"t(\d+):(mention|link|node|edge):(.+)", target)
    require(match and int(match[1]) == entry["source_turn_id"], "invalid retraction target")
    kind, value = match[2], match[3]
    removed_mids, removed_links, removed_paths = set(), set(), set()
    if kind == "mention":
        require(value in active["mentions"], "inactive retraction mention")
        removed_mids.add(value)
    elif kind == "link":
        require(value in active["links"], "inactive retraction link")
        removed_links.add(value)
    elif kind == "node":
        require(value in arg["nodes"], "unknown retraction node")
        removed_mids.update(m["mid"] for m in arg["investor_mentions"]["mentions"]
                            if m["node_id"] == value and m["mid"] in active["mentions"])
        require(removed_mids, "inactive retraction node")
    else:
        require(value in arg["edges"], "unknown retraction edge")
        removed_paths.update(pid for pid in active["paths"] if value in arg["paths"][pid]["edges"])
        require(removed_paths, "inactive retraction edge")
    removed_links.update(lid for lid in active["links"] if
                         arg["explicit_links"][lid]["from"] in removed_mids or
                         arg["explicit_links"][lid]["to"] in removed_mids)
    removed_paths.update(pid for pid in active["paths"] if
                         arg["paths"][pid]["from_mid"] in removed_mids or
                         arg["paths"][pid]["to_mid"] in removed_mids or
                         arg["paths"][pid]["link_id"] in removed_links)
    active["mentions"] = [x for x in active["mentions"] if x not in removed_mids]
    active["links"] = [x for x in active["links"] if x not in removed_links]
    active["paths"] = [x for x in active["paths"] if x not in removed_paths]
    active["sides"] = [x for x in active["sides"] if arg["side_checks"][x]["path_id"] not in removed_paths]
    active["node_uses"] = [x for x in active["node_uses"] if not
                           (x.startswith("mention:") and x.removeprefix("mention:") in removed_mids)
                           and not any(x.startswith(pid + ":") for pid in removed_paths)]
    active["edge_uses"] = [x for x in active["edge_uses"] if not any(x.startswith(pid + ":") for pid in removed_paths)]
    active["connections"] = [i for i in active["connections"] if
                             arg["ground_claim_connections"][i]["ground_mid"] not in removed_mids and
                             arg["ground_claim_connections"][i]["claim_mid"] not in removed_mids and
                             all(seg["link_id"] not in removed_links and
                                 not any(pid in removed_paths for pid in arg["searches"][seg["search_id"]]["path_ids"])
                                 for seg in arg["ground_claim_connections"][i]["segments"])]
    active["groups"] = [gid for gid in active["groups"] if all(
        g["group_id"] != gid or not (g["target_mid"] in removed_mids or set(g["members"]) & removed_mids)
        for g in arg["logical_groups"])]


def attach_lineage(out, prior, prior_hash, prior_path, raw_s4, suppressed):
    require(prior["session_id"] == out["session_id"] and prior["turn_id"] + 1 == out["turn_id"]
            and prior["graph_version"] == out["graph_version"]
            and prior["graph_schema_sha256"] == out["graph_schema_sha256"], "previous argument mismatch")
    lineage = deepcopy(prior.get("lineage", []))
    snapshot = {k: deepcopy(v) for k, v in prior.items() if k not in ("lineage", "active_history")}
    lineage.append({"source_turn_id": prior["turn_id"], "argument_sha256": prior_hash,
                    "argument_path": str(prior_path), "argument": snapshot, "active": active_ids(prior)})
    # The preceding turn may already carry retired references from older turns.
    if prior.get("lineage"):
        lineage[-1]["active"] = deepcopy(prior.get("current_active", active_ids(prior)))
    active_history = []
    for address in raw_s4["addresses"]:
        if address["type"] == "retraction":
            match = re.fullmatch(r"t(\d+):(mention|link|node|edge):(.+)", address["target"])
            require(match, "invalid retraction reference")
            entry = next((e for e in lineage if e["source_turn_id"] == int(match[1])), None)
            require(entry is not None, "retraction source turn missing")
            retire(entry, address["target"])
            active_history.append({"type": "retraction", "target": address["target"],
                                   "source_turn_id": entry["source_turn_id"]})
        elif address["type"] == "repeat":
            active_history.append({"type": "repeat", "target": address["target"],
                                   "source_turn_id": address.get("target_turn_id", prior["turn_id"])})
    out.update(based_on_turn=prior["turn_id"], prior_argument_sha256=prior_hash,
               prior_argument_path=str(prior_path), lineage=lineage, active_history=active_history,
               current_active=active_ids(out), s4_extraction=raw_s4, suppressed_mids=suppressed)
    return out
