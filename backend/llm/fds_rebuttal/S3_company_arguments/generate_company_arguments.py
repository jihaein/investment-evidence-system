# 프로토타입 구현을 위해 미승인(draft) 화살표 카드도
# S3 기업 논증 생성에 사용함.
# 이는 카드의 승인이나 인과관계 입증을 의미하지 않음.
"""Generate the latest 120-company S3 snapshots from the project's 데이터 CSV."""
import argparse
import json
import sys
import tempfile
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.dont_write_bytecode = True
from .company_argument_adapters import FORMULAS, Inputs, Period, Point, cutoff_for, digest, file_hash
from .company_argument_engine import (Graph, build_company, choose_series, edge_argument, graph_identity, index,
                    node_argument, read_json)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MONTHS = (4, 6, 9, 12)
NODE_STATES = ("observed", "missing", "unobservable")
EDGE_STATES = ("held", "not_held", "no_change", "no_data")


def ensure(ok, message):
    if not ok:
        raise ValueError(message)


def update_period(day):
    ensure(day.day == 1 and day.month in MONTHS, f"unsupported update date: {day}")
    slot = MONTHS.index(day.month) + 1
    next_day = date(day.year + 1, 4, 1) if slot == 4 else date(day.year, MONTHS[slot], 1)
    return f"company.argument{day.year}-{slot}.json", (next_day - timedelta(days=1)).isoformat()


def compare_peer(graph, peer_graph, investor):
    report = {"status": "unverified", "reason": "S4/S5 directories excluded"}
    if peer_graph:
        peer = read_json(peer_graph)
        nodes, edges = index(peer["nodes"], "node_id"), index(peer["edges"], "edge_id")
        checks = {"version": peer["graph_version"] == graph.version, "node_ids": nodes.keys() == graph.nodes.keys(),
                  "edge_ids": edges.keys() == graph.edges.keys(),
                  "graph_content": graph_identity(peer) == graph.meta["schema_sha256"]}
        ensure(all(checks.values()), f"peer graph mismatch: {checks}")
        report = {"status": "graph_verified", "checks": checks}
    if investor:
        arg = read_json(investor)
        ensure(arg["graph_version"] == graph.version, "investor graph_version mismatch")
        for k, ids in [("nodes", graph.nodes), ("edges", graph.edges)]:
            ensure(isinstance(arg[k], dict), f"investor {k} must be keyed by graph ID")
            unknown = arg[k].keys() - ids.keys()
            ensure(not unknown, f"investor {k} contains unknown IDs: {sorted(unknown)}")
        report["investor_subset_join"] = {"node_keys": len(arg["nodes"]), "edge_keys": len(arg["edges"])}
    return report


def check_config(cfg):
    start, end = cfg["years"]
    ensure(1 <= start <= end < 9999 and cfg["lookback_observations"] >= 2, "invalid execution range")
    ensure(1 <= cfg["fiscal_year_end_month"] <= 12, "invalid fiscal year-end month")
    last = date.fromisoformat(cfg["last_update"])
    ensure(date(start, 4, 1) <= last <= date(end, 12, 1) and last.day == 1
           and last.month in MONTHS, "invalid last_update")
    for policy in [*cfg["trend_policies"].values(), *(p for p in cfg.get("node_trends", {}).values() if p is not None)]:
        ensure(policy["method"] in ("relative", "absolute", "percentage_points", "ordinal")
               and 0 <= policy["threshold"] < float("inf"), "invalid trend policy")
        if policy["method"] == "ordinal":
            ensure(len(set(policy["levels"])) == len(policy["levels"]) >= 2, "invalid ordinal scale")


def baseline_summary(cfg, g, data):
    overrides = {}
    for nid, reason in data.overrides.items():
        if nid in data.confirmed and not g.nodes[nid]["observable"]:
            sources = [name for name, spec in cfg["sources"].items()
                       if any(m["node_id"] == nid for m in spec["mappings"])]
            sources.extend("formula:" + f["function"] for f in cfg["formulas"] if f["node_id"] == nid)
            overrides[nid] = {"node_id": nid, "graph_observable": False, "s3_observable": True,
                              "reason": reason, "sources": sources}
    # Exercise the real graph with empty series, without inventing a company or a fact.
    empty = {n: node_argument(n, [], None, v["observable"] or n in overrides, [])
             for n, v in g.nodes.items()}
    blank_edges = [edge_argument(edge, g.cards[eid], empty, {}, {}) for eid, edge in g.edges.items()]
    ensure(all(e["status"] == "no_data" and not e["triggered"] for e in blank_edges)
           and sum(len(e["unverifiable"]) for e in blank_edges) == g.meta["counterconditions"],
           "empty-data conditions failed")
    json.dumps([empty, blank_edges], allow_nan=False)
    unmatched = {n: {"label": v["label_ko"], "graph_observable": v["observable"],
                     "reason": "formula_inputs_unavailable" if n in data.units else "no_verified_mapping"}
                 for n, v in g.nodes.items() if n not in data.confirmed}
    for source, info in data.audit.items():
        raw = [p for seq in data.raw.values() for p in seq if p.source == source]
        if raw:
            info["period_end_range"] = [min(p.period.end for p in raw), max(p.period.end for p in raw)]
            info["available_at_range"] = [min(p.available_at for p in raw), max(p.available_at for p in raw)]
    return {"graph": dict(g.meta, file=cfg["graph"], cards_directory=cfg["cards"]),
            "mapping_version": cfg["mapping_version"], "config_sha256": digest(cfg),
            "company_list": cfg["company_list"], "companies": data.companies,
            "update_years": cfg["years"], "sources": data.audit, "source_exclusions": data.rejections,
            "verified_node_ids": sorted(data.confirmed), "observable_overrides": overrides,
            "unconnected_nodes": unmatched, "deferred": cfg["deferred"],
            "empty_data_validation": {"node_states": dict(Counter(n["status"] for n in empty.values())),
                                      "no_data": len(blank_edges), "unverifiable": g.meta["counterconditions"]},
            "peer_compatibility": {"status": "unverified", "reason": "S4/S5 directories excluded"}}


def dump(path, obj):
    with path.open("x", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, allow_nan=False, indent=2)
        f.write("\n")


def compact(obj):
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def write_argument(path, header, companies):
    # One line per node/edge keeps large snapshots navigable.
    with path.open("x", encoding="utf-8") as out:
        out.write("{\n")
        for k, v in header.items():
            out.write(f"  {compact(k)}: {compact(v)},\n")
        out.write('  "companies": {\n')
        for i, (code, arg) in enumerate(companies.items()):
            if i:
                out.write(",\n")
            out.write(f"    {compact(code)}: {{\n")
            for k, v in arg.items():
                if k not in ("nodes", "edges"):
                    out.write(f"      {compact(k)}: {compact(v)},\n")
            for field in ("nodes", "edges"):
                out.write(f"      {compact(field)}: {{\n")
                out.write(",\n".join(f"        {compact(k)}: {compact(v)}" for k, v in arg[field].items()))
                out.write("\n      }" + (",\n" if field == "nodes" else "\n"))
            out.write("    }")
        out.write("\n  }\n}\n")


def collect_facts(ids, pool, registry):
    for fid in ids:
        p = pool[fid]
        if fid in registry:
            ensure(registry[fid] == p, f"fact ID collision: {fid}")
            continue
        registry[fid] = p
        collect_facts(p.input_fact_ids, pool, registry)


def card_index(graph):
    return {eid: {"edge_type": e["edge_type"], "sign": e["sign"],
                  "empirical_sign": graph.cards[eid].get("empirical_sign"),
                  "review_status": graph.cards[eid]["review_status"]} for eid, e in graph.edges.items()}


def generate(day, target, graph, data, report, registry):
    filename, end = update_period(day)
    companies, issues = {}, {}
    for code in data.companies:
        arg, pool, diagnostics = build_company(code, day, graph, data)
        for n in arg["nodes"].values():
            collect_facts(n["fact_ids"], pool, registry)
        companies[code] = arg
        issues[code] = [{"node_id": n, "reason": r, "count": c} for (n, r), c in sorted(diagnostics.items())]
    header = {"graph_version": graph.version, "as_of": day.isoformat(), "effective_from": day.isoformat(),
              "effective_to": end, "draft_cards_allowed": True,
              "s3_metadata": {"graph_sha256": graph.meta["graph_sha256"],
                              "graph_schema_sha256": graph.meta["schema_sha256"],
                              "cards_sha256": graph.meta["cards_sha256"], "config_sha256": report["config_sha256"],
                              "mapping_version": report["mapping_version"], "fact_registry": "fact_registry.json",
                              "cutoff": (day-timedelta(days=1)).isoformat() + "T23:59:59+09:00",
                              "interpretation": "검수 전 카드 기준의 관측 방향 일치 여부. 인과 검증 및 S6 승인 전.",
                              "observable_overrides": report["observable_overrides"],
                              "edge_cards": card_index(graph)}}
    write_argument(target / filename, header, companies)
    return issues


def load_registry(path, data):
    points = {}
    raw_ids = {p.fact_id for series in data.raw.values() for p in series}
    q4_rows = {(v["company_id"], v["node_id"], v["evidence"]["line"], v["value"])
               for rows in data.q4.values() for v in rows}
    for fid, record in read_json(path)["facts"].items():
        key, end = record.pop("fact_id"), record.pop("period_end")
        record["period"] = Period(**record["period"])
        record["input_fact_ids"] = tuple(record["input_fact_ids"])
        p = Point(**record)
        ensure(p.fact_id == fid == key and end == p.period.end, f"registry integrity: {fid}")
        points[fid] = p
    for p in points.values():
        if not p.input_fact_ids:
            ensure(p.fact_id in raw_ids, f"cannot locate source observation: {p.fact_id}")
        parents = [points[fid] for fid in p.input_fact_ids]
        for fid in p.input_fact_ids:
            dep = points[fid]
            ensure(dep.company_id == p.company_id and dep.corp_code == p.corp_code
                   and dep.available_at <= p.available_at and dep.basis == p.basis,
                   f"invalid lineage: {p.fact_id}")
            if p.source == "formula":
                ensure(dep.period == p.period, f"unaligned formula input: {p.fact_id}")
        if parents:
            ensure(p.available_at == max(v.available_at for v in parents), "wrong derived publication date")
            if p.source == "formula":
                fn = FORMULAS[p.evidence["function"]][1]
                ensure(p.value == fn(*(v.value for v in parents)), f"formula mismatch: {p.fact_id}")
            else:
                ensure((p.company_id, p.node_id, p.evidence["line"], p.value) in q4_rows
                       and len(parents) == 4 and p.value == parents[0].value - sum(v.value for v in parents[1:]),
                       f"quarter reconciliation failed: {p.fact_id}")
    return points


def validate_snapshot(path, day, graph, data, registry):
    doc = read_json(path)
    filename, end = update_period(day)
    ensure((path.name, doc["as_of"], doc["effective_from"], doc["effective_to"]) ==
           (filename, day.isoformat(), day.isoformat(), end), "invalid snapshot dates")
    ensure(doc["graph_version"] == graph.version and doc["draft_cards_allowed"] is True
           and doc["s3_metadata"]["edge_cards"] == card_index(graph), "snapshot graph/card mismatch")
    ensure(doc["companies"].keys() == data.companies.keys(), "company roster mismatch")
    counts = Counter(dict.fromkeys((*NODE_STATES, *EDGE_STATES, "triggered", "unverifiable",
                                    "verified_not_triggered", "observed_without_direction"), 0))
    by_node = Counter()
    for code, arg in doc["companies"].items():
        ensure(arg["company_id"] == code and arg["as_of"] == doc["as_of"]
               and arg["corp_code"] == data.companies[code]["corp_code"], "wrong company or date")
        ensure(arg["nodes"].keys() == graph.nodes.keys() and arg["edges"].keys() == graph.edges.keys(),
               "graph ID coverage mismatch")
        series, policies = {}, {}
        eligible, _, _ = data.resolve(code, day)
        for nid, n in arg["nodes"].items():
            ensure(n["status"] in NODE_STATES, f"invalid node state: {nid}")
            points = [registry[fid] for fid in n["fact_ids"]]
            ensure(points == choose_series(eligible[nid].values(), data.config["lookback_observations"]),
                   f"eligible revision or period selection mismatch: {code}/{nid}")
            series[nid] = points
            ensure(len(n["series"]) == len(points) and list(n["series"].items()) ==
                   [(p.period.end, p.value) for p in points], f"series/registry mismatch: {code}/{nid}")
            ensure(all(p.node_id == nid and p.company_id == code and p.corp_code == arg["corp_code"]
                       and p.available_at <= cutoff_for(day) and p.period.end <= p.available_at[:10]
                       for p in points), f"point-in-time/identity mismatch: {code}/{nid}")
            obs = graph.nodes[nid]["observable"] or (nid in data.confirmed and nid in data.overrides)
            if nid in data.config.get("node_trends", {}):
                policy = data.config["node_trends"][nid]
            else:
                policy = data.config["trend_policies"].get(points[0].unit) if points else None
            policies[nid] = policy
            expected = node_argument(nid, points, policy, obs, n.get("reason", []))
            ensure(n == expected, f"node state/direction mismatch: {code}/{nid}")
            counts[n["status"]] += 1
            if points:
                by_node[nid] += 1
                counts["observed_without_direction"] += int(n["direction"] is None)
        ends = [p.period.end for seq in series.values() for p in seq]
        ensure(arg["period"] == (f"{min(ends)}/{max(ends)}" if ends else None), "incorrect company period")
        for eid, e in arg["edges"].items():
            card = graph.cards[eid]
            cc_ids = {cc["cc_id"] for cc in card["counterconditions"]}
            tr, uv = set(e["triggered"]), set(e["unverifiable"])
            ensure(not tr & uv and tr | uv <= cc_ids and len(tr) == len(e["triggered"])
                   and len(uv) == len(e["unverifiable"]), f"invalid condition IDs: {code}/{eid}")
            ensure(e == edge_argument(graph.edges[eid], card, arg["nodes"], series, policies),
                   f"edge/condition direction mismatch: {code}/{eid}")
            counts[e["status"]] += 1
            counts["triggered"] += len(tr)
            counts["unverifiable"] += len(uv)
            counts["verified_not_triggered"] += len(cc_ids) - len(tr) - len(uv)
    return {"filename": filename, "sha256": file_hash(path), "counts": dict(counts),
            "observed_companies_per_node": dict(sorted(by_node.items()))}


def run_batch(mode, dates, graph, data, report):
    runs = HERE / "runs"
    runs.mkdir(exist_ok=True)
    label = f"one-{dates[0]}" if mode == "one" else f"full-{dates[0].year}-{dates[-1].year}"
    # Publish only a completely validated run. Existing runs are never overwritten.
    with tempfile.TemporaryDirectory(prefix=".building-", dir=runs) as tmp:
        stage = Path(tmp)
        registry, issues = {}, {}
        for day in dates:
            issues[day.isoformat()] = generate(day, stage, graph, data, report, registry)
        dump(stage / "fact_registry.json", {"schema_version": 1, "facts": {
            fid: p.record() for fid, p in sorted(registry.items())}})
        checked = load_registry(stage / "fact_registry.json", data)
        report["fact_count"] = len(checked)
        report["by_update"] = {}
        totals = Counter()
        for day in dates:
            filename, _ = update_period(day)
            summary = validate_snapshot(stage / filename, day, graph, data, checked)
            report["by_update"][day.isoformat()] = summary
            totals.update(summary["counts"])
        report.update(totals=dict(totals), resolution_diagnostics=issues, validation="passed")
        dump(stage / "run_summary.json", report)
        suffix = 1
        while True:
            dest = runs / (label if suffix == 1 else f"{label}-{suffix}")
            if not dest.exists():
                try:
                    stage.rename(dest)
                    return dest
                except FileExistsError:
                    pass
            suffix += 1


def main():
    parser = argparse.ArgumentParser(description="S3 company argument (KST point-in-time)")
    parser.add_argument("--config", type=Path, default=HERE / "company_argument_config.json")
    modes = parser.add_subparsers(dest="mode", required=True)
    inspect = modes.add_parser("inspect")
    inspect.add_argument("--peer-graph", type=Path, help="Optional permitted copy of the S5 source graph")
    inspect.add_argument("--peer-argument", type=Path, help="Optional permitted copy of the S5 output")
    modes.add_parser("one").add_argument("as_of", type=date.fromisoformat)
    modes.add_parser("all")
    args = parser.parse_args()
    cfg = read_json(args.config)
    check_config(cfg)
    graph = Graph(ROOT / cfg["graph"], ROOT / cfg["cards"])
    data = Inputs(ROOT, cfg, graph.nodes)
    report = baseline_summary(cfg, graph, data)
    if args.mode == "inspect":
        report["peer_compatibility"] = compare_peer(graph, args.peer_graph, args.peer_argument)
        print(json.dumps({k: v for k, v in report.items() if k not in ("unconnected_nodes", "source_exclusions")},
                         ensure_ascii=False, indent=2))
        updates = sum(date(year, month, 1).isoformat() <= cfg["last_update"]
                      for year in range(cfg["years"][0], cfg["years"][1]+1) for month in MONTHS)
        print(f"unconnected={len(report['unconnected_nodes'])}, exclusions={len(data.rejections)}, "
              f"updates_possible={updates if data.companies else 0}")
        return
    if not data.companies:
        print("Structure validated. Company roster unavailable. No company JSON generated.")
        return
    if args.mode == "one":
        ensure(date(cfg["years"][0], 4, 1) <= args.as_of <= date.fromisoformat(cfg["last_update"]),
               "as_of outside configured update range")
    dates = [args.as_of] if args.mode == "one" else [
        date(year, month, 1) for year in range(cfg["years"][0], cfg["years"][1]+1)
        for month in MONTHS if date(year, month, 1).isoformat() <= cfg["last_update"]]
    dest = run_batch(args.mode, dates, graph, data, report)
    print(json.dumps({"run": str(dest), "updates": len(dates), "companies": len(data.companies),
                      "facts": report["fact_count"], "totals": report["totals"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
