"""Check the published S5 contract and optional S3 key join. No evidence tags."""
from collections import Counter

from .graph_loader import propagate, require


def verify_route(nodes, edges, graph, start, end):
    require(nodes and nodes[0] == start and nodes[-1] == end and edges
            and len(edges) + 1 == len(nodes) == len(set(nodes)), "invalid route endpoints or length")
    for a, eid, b in zip(nodes, edges, nodes[1:]):
        edge = graph.edges[eid]
        require((edge["cause"], edge["effect"]) == (a, b) and edge["edge_type"] != "signal",
                "invalid causal edge")


def verify(out, graph, s4):
    require(out["investor_mentions"] == s4 and out["graph_version"] == graph.version
            and out["graph_schema_sha256"] == graph.fingerprint, "source or graph mismatch")
    mentions = {m["mid"]: m for m in s4["mentions"]}
    mappings, paths, searches = out["mappings"], out["paths"], out["searches"]
    require(out["grounds"] == [mid for mid, m in mentions.items() if m["role"] == "grounds"],
            "ground coverage")
    require(mappings.keys() == mentions.keys() and out["unmapped"] ==
            [m for mid, m in mentions.items() if mappings[mid]["status"] == "unmapped"], "mention coverage")
    claims = [mappings[mid] for mid, m in mentions.items() if m["role"] == "claim"]
    require(out["claims"] == claims and out["claim_node"] ==
            (claims[0]["node_id"] if len(claims) == 1 and claims[0]["status"] == "mapped" else None),
            "claim coverage")
    links = out["explicit_links"]
    require(len(links) == len(s4["links"]), "explicit link loss")
    for i, original in enumerate(s4["links"], 1):
        link = links[f"L{i}"]
        require(all(link[k] == v for k, v in original.items()) and link["link_id"] == f"L{i}"
                and link["origin"] == "explicit" and link["source"] == "s4_link", "explicit source overwritten")
        if link["status"] != "invalid_reference":
            search = searches[link["search_id"]]
            require(search["link_id"] == link["link_id"] and search["path_ids"] == link["path_ids"]
                    and (search["from_mid"], search["to_mid"]) == (link["from"], link["to"])
                    and search["status"] == link["status"], "link/search mismatch")
    found = [pid for s in searches.values() for pid in s["path_ids"]]
    require(Counter(found) == Counter(paths.keys()), "path selection mismatch")
    for s in searches.values():
        require(s["selected_count"] == len(s["path_ids"]) <= out["config"]["max_paths_per_segment"]
                and s["excluded_count"] == len(s["excluded"])
                and s["found_count"] == s["selected_count"] + s["excluded_count"], "search counts")
        a, b = mappings[s["from_mid"]], mappings[s["to_mid"]]
        require(s["tentative"] == (a["status"] != "mapped" or b["status"] != "mapped"), "search uncertainty")
        selected = [paths[pid] for pid in s["path_ids"]]
        ranks = [p["rank"] for p in selected + s["excluded"]]
        require(sorted(ranks) == list(range(1, s["found_count"] + 1)), "candidate rank coverage")
        audit = s.get("selection")
        if "llm_enabled" in out["config"]:
            require(isinstance(audit, dict), "missing selection audit")
        if audit is not None:
            ids = [f"C{i}" for i in range(1, s["found_count"] + 1)]
            chosen = [f"C{p['rank']}" for p in selected]
            called = bool(out["config"].get("llm_enabled")) and len(ids) >= 2
            require(audit["called"] == called and audit["model"] == (out["config"]["llm_model"] if called else None)
                    and audit["selected"] == chosen
                    and set(audit["reasons"]) == set(ids), "selection audit mismatch")
            if audit["method"] == "llm":
                require(called and not audit["fallback"] and 1 <= len(chosen) <= 2, "invalid LLM selection")
            else:
                require(audit["method"] == "python" and audit["fallback"] == called
                        and (not called or audit.get("fallback_reason"))
                        and chosen == ids[:out["config"]["max_paths_per_segment"]], "invalid Python selection")
            for p in selected + s["excluded"]:
                reason = audit["reasons"][f"C{p['rank']}"]
                require(isinstance(reason, str) and reason.strip()
                        and reason == p.get("selection_reason", p.get("reason")), "candidate reason mismatch")
        else:
            require(ranks == list(range(1, s["found_count"] + 1)), "invalid legacy candidate order")
        for candidate in s["excluded"]:
            verify_route(candidate["nodes"], candidate["edges"], graph, a["node_id"], b["node_id"])
        for alt in s["alternatives"]:
            start, end = alt["from_node"], alt["to_node"]
            require(start in a["valid_candidates"] and end in b["valid_candidates"] and alt["tentative"]
                    and (start, end) != (a["node_id"], b["node_id"]), "invalid alternate mapping")
            if alt["status"] == "candidate_only":
                verify_route(alt["nodes"], alt["edges"], graph, start, end)

    node_uses, edge_uses, sides = {}, {}, out["side_checks"]
    for entries, allowed, uses in ((out["nodes"], graph.nodes, node_uses), (out["edges"], graph.edges, edge_uses)):
        for key, entry in entries.items():
            require(key in allowed and "origin" not in entry and entry["uses"], "invalid graph item")
            for use in entry["uses"]:
                uid = use["use_id"]
                require(uid not in uses, f"duplicate use_id {uid}")
                uses[uid] = (key, use)
    for mid, m in mentions.items():
        if m["node_id"] in graph.nodes:
            node, use = node_uses.pop("mention:" + mid)
            require(node == m["node_id"] and use["origin"] == "explicit"
                    and all(use[k] == v for k, v in m.items()), "explicit mention loss")
    for pid, p in paths.items():
        ns, es = p["nodes"], p["edges"]
        s = searches[p["search_id"]]
        start, end = mentions[s["from_mid"]], mentions[s["to_mid"]]
        require(p["path_id"] == pid and pid in s["path_ids"] and p["from_mid"] == start["mid"]
                and p["to_mid"] == end["mid"] and p["tentative"] == s["tentative"]
                and p["rank"] >= 1, f"invalid path {pid}")
        verify_route(ns, es, graph, start["node_id"], end["node_id"])
        origin = ("explicit" if len(es) == 1 else "bridged") if s["link_id"] else "chain"
        require(p["origin"] == origin and p["link_id"] == s["link_id"], "path provenance")
        directions = [start["direction"]]
        for i, eid in enumerate(es, 1):
            edge = graph.edges[eid]
            require(all(out["edges"][eid][k] == v for k, v in edge.items()), "graph edge changed")
            directions.append(propagate(directions[-1], edge["sign"]))
            key, use = edge_uses.pop(f"{pid}:E{i}")
            require(key == eid and use["path_id"] == pid and use["origin"] == origin
                    and (use["from_mid"], use["to_mid"], use["link_id"]) == (p["from_mid"], p["to_mid"], p["link_id"])
                    and use["horizon"] == p["horizon"] == end["horizon"] and use["tentative"] == p["tentative"]
                    and use["expected_direction"] == directions[-1]
                    and use["cause_direction"] == directions[-2], "edge use mismatch")
        require(directions == p["expected_directions"], "sign propagation mismatch")
        for i, nid in enumerate(ns):
            key, use = node_uses.pop(f"{pid}:N{i}")
            endpoint = i in (0, len(ns) - 1)
            require(key == nid and use["path_id"] == pid and use["origin"] == ("explicit" if endpoint else origin)
                    and (use["from_mid"], use["to_mid"], use["link_id"]) == (p["from_mid"], p["to_mid"], p["link_id"])
                    and use["expected_direction"] == directions[i]
                    and use["horizon"] == (start["horizon"] if i == 0 else end["horizon"])
                    and use["tentative"] == p["tentative"], "node use mismatch")
            if endpoint:
                require(use["mid"] == (start if i == 0 else end)["mid"], "wrong endpoint mention")
        picked = [c["side_id"] for c in p["side_candidates"] if c["reason"] == "selected"]
        require(set(picked) == {k for k, v in sides.items() if v["path_id"] == pid}
                and len(picked) <= out["config"]["max_side_per_path"], "side selection mismatch")
    for sid, c in sides.items():
        p, e = paths[c["path_id"]], graph.edges[c["via_edge"]]
        expected = p["expected_directions"][p["nodes"].index(c["target_node"])]
        require(c["side_id"] == sid and c["horizon"] == p["horizon"] and c["tentative"] == p["tentative"]
                and c["target_expected_direction"] == expected and expected in ("+", "-")
                and c["node_id"] == e["cause"] and e["effect"] == c["target_node"]
                and c["node_id"] not in p["nodes"] and e["edge_type"] != "signal"
                and c["required"] == ("not_down" if expected == e["sign"] else "not_up"), "invalid side premise")
        key, use = node_uses.pop(sid)
        require(key == c["node_id"] and all(use[k] == v for k, v in c.items()), "side use mismatch")
    require(not node_uses and not edge_uses, "orphan uses")
    pairs = []
    for connection in out["ground_claim_connections"]:
        cur = connection["ground_mid"]
        pairs.append((cur, connection["claim_mid"]))
        for seg in connection["segments"]:
            q = searches[seg["search_id"]]
            require(cur == q["from_mid"] == seg["from_mid"] and q["to_mid"] == seg["to_mid"]
                    and seg["link_id"] == q["link_id"] and (seg["kind"] == "link") == bool(q["link_id"]),
                    "broken ground/claim segment")
            cur = seg["to_mid"]
        require(cur == connection["claim_mid"], "unresolved connection endpoint")
        steps = [searches[seg["search_id"]] for seg in connection["segments"]]
        require(connection["explicit_reachable"] == all(q["link_id"] is not None for q in steps)
                and connection["graph_connected"] == all(q["status"] == "connected" for q in steps)
                and connection["tentative"] == any(q["tentative"] for q in steps), "connection status")
    require(Counter(pairs) == Counter((g, c["mid"]) for g in out["grounds"] for c in claims),
            "ground/claim pair coverage")
    for g in out["logical_groups"]:
        require(g["operator"] in ("AND", "OR", "UNSPECIFIED") and g["target_mid"] in mentions
                and set(g["members"]) <= set(out["grounds"]), "logical group reference")
    groups = {g["group_id"] for g in out["logical_groups"]}
    for issue in out["unresolved"]:
        for field, index in (("mid", mentions), ("from_mid", mentions), ("to_mid", mentions),
                             ("search_id", searches), ("link_id", links), ("path_id", paths),
                             ("group_id", groups), ("node_id", out["nodes"])):
            if field in issue:
                require(issue[field] in index, f"unresolved reference: {field}")
        require(set(issue.get("group_ids", [])) <= groups, "unresolved group references")
        for pids in issue.get("paths_by_direction", {}).values():
            require(set(pids) <= paths.keys(), "unresolved path references")


def verify_company_join(out, company):
    require(company["graph_version"] == out["graph_version"] and
            company["s3_metadata"]["graph_schema_sha256"] == out["graph_schema_sha256"],
            "S3 graph_version or graph content differs")
    edges = set(out["edges"]) | {s["via_edge"] for s in out["side_checks"].values()}
    require(company["companies"], "empty S3 roster")
    for code, arg in company["companies"].items():
        require(out["nodes"].keys() <= arg["nodes"].keys() and edges <= arg["edges"].keys(),
                f"S3 graph key missing for {code}")
    return len(company["companies"])
