"""Graph identity, comparable observation windows and S3 state calculations."""
import json
import math
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal

from .company_argument_adapters import digest, file_hash


def read_json(path):
    def unique(pairs):
        obj = dict(pairs)
        if len(pairs) != len(obj):
            raise ValueError(f"duplicate JSON key: {path}")
        return obj
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique)


def index(records, field):
    result = {}
    for item in records:
        key = item[field]
        if not isinstance(key, str) or not key.strip() or key in result:
            raise ValueError(f"empty/duplicate {field}: {key}")
        result[key] = item
    return result


def graph_identity(graph):
    return digest({"graph_version": graph["graph_version"],
                   "nodes": index(graph["nodes"], "node_id"),
                   "edges": index(graph["edges"], "edge_id")})


class Graph:
    def __init__(self, graph_path, cards_dir):
        raw = read_json(graph_path)
        self.version = raw["graph_version"]
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("missing graph_version")
        self.nodes = index(raw["nodes"], "node_id")
        self.edges = index(raw["edges"], "edge_id")
        if not self.nodes or not self.edges:
            raise ValueError("empty graph")
        for node in self.nodes.values():
            if not isinstance(node["observable"], bool):
                raise ValueError(f"invalid observable: {node['node_id']}")
        self.cards, hashes, warnings = {}, {}, []
        for path in sorted(cards_dir.glob("*.json")):
            card = read_json(path)
            eid = card["edge_id"]
            if eid in self.cards or eid not in self.edges:
                raise ValueError(f"duplicate/orphan card: {eid}")
            edge = self.edges[eid]
            if (edge["cause"] not in self.nodes or edge["effect"] not in self.nodes
                    or edge["sign"] not in ("+", "-") or card["sign"] != edge["sign"]
                    or not edge["edge_type"] or not card["review_status"]):
                raise ValueError(f"invalid edge/card fields: {eid}")
            if not isinstance(card["counterconditions"], list):
                raise ValueError(f"invalid counterconditions: {eid}")
            for cc in index(card["counterconditions"], "cc_id").values():
                if cc["check_node"] is not None and cc["check_node"] not in self.nodes:
                    warnings.append({"edge_id": eid, "cc_id": cc["cc_id"],
                                     "unknown_check_node": cc["check_node"]})
                if cc["check_direction"] not in ("+", "-", "0", None):
                    raise ValueError(f"invalid check_direction: {eid} {cc['cc_id']}")
            self.cards[eid] = card
            hashes[path.name] = file_hash(path)
        if self.cards.keys() != self.edges.keys():
            raise ValueError(f"missing cards: {sorted(self.edges.keys() - self.cards.keys())}")
        cc = [item for card in self.cards.values() for item in card["counterconditions"]]
        self.meta = {"version": self.version, "graph_sha256": file_hash(graph_path),
                     "schema_sha256": graph_identity(raw), "cards_sha256": digest(hashes),
                     "nodes": len(self.nodes), "edges": len(self.edges), "card_files": len(hashes),
                     "observable_true": sum(n["observable"] for n in self.nodes.values()),
                     "source_variable_set": sum(n.get("source_variable") is not None for n in self.nodes.values()),
                     "review_status": dict(Counter(c["review_status"] for c in self.cards.values())),
                     "counterconditions": len(cc), "null_check_node": sum(c["check_node"] is None for c in cc),
                     "null_check_direction": sum(c["check_direction"] is None for c in cc),
                     "reference_warnings": warnings}


def consecutive(a, b):
    p, q = a.period, b.period
    if (p.frequency, a.unit, a.measure, a.basis) != (q.frequency, b.unit, b.measure, b.basis):
        return False
    if q.ordinal != p.ordinal + 1:
        return False
    days = (date.fromisoformat(q.end) - date.fromisoformat(p.end)).days
    if p.frequency == "filing_event":
        return 300 <= days <= 430  # consecutive FY disclosure-date market samples
    if p.start != p.end or q.start != q.end:
        return (date.fromisoformat(q.start) - date.fromisoformat(p.end)).days == 1
    limits = {"daily": (1, 1), "monthly": (28, 31), "quarterly": (89, 92),
              "annual": (365, 366)}
    low, high = limits[p.frequency]
    return low <= days <= high


def pct_change(start, end):
    """Design v4 stores a fraction. Negative bases use their magnitude."""
    if start == 0:
        return None
    start, end = Decimal(str(start)), Decimal(str(end))
    change = float((end - start) / abs(start))
    return change if math.isfinite(change) else None


def pair_direction(a, b, policy):
    if not policy:
        return None
    method, threshold = policy["method"], policy["threshold"]
    x, y = a.value, b.value
    if method == "ordinal":
        levels = policy["levels"]
        if x not in levels or y not in levels:
            return None
        delta = levels.index(y) - levels.index(x)
    else:
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return None
        x, y = Decimal(str(x)), Decimal(str(y))
        if method == "relative":
            if x == 0:
                return "+" if y > 0 else "-" if y < 0 else "0"
            delta = (y-x) / abs(x)
        elif method == "percentage_points":
            delta = (y-x) * 100
        elif method == "absolute":
            delta = y-x
        else:
            raise ValueError(f"unknown trend method: {method}")
    return "0" if delta == 0 or abs(delta) < Decimal(str(threshold)) else "+" if delta > 0 else "-"


def overall_direction(points, policy):
    if len(points) < 2 or not all(consecutive(a, b) for a, b in zip(points, points[1:])):
        return None
    return pair_direction(points[0], points[-1], policy)


def period_directions(points, policy):
    comparisons = []
    for a, b in zip(points, points[1:]):
        if consecutive(a, b):
            direction = pair_direction(a, b, policy)
            if direction is not None:
                comparisons.append(([a.period.end, b.period.end], direction))
    return comparisons


def choose_series(points, limit):
    """Use freshest observed period, prefer annual at equal end, then latest filing."""
    if not points:
        return []
    points = list(points)
    newest = max(points, key=lambda p: (p.period.end, p.period.frequency == "annual",
                                        p.available_at, -p.priority, p.fact_id))
    same_frequency = [p for p in points if p.period.frequency == newest.period.frequency]
    latest_by_end = {}
    for p in sorted(same_frequency, key=lambda p: (p.available_at, -p.priority, p.fact_id)):
        latest_by_end[p.period.end] = p
    cohort = []
    for end in sorted(latest_by_end, reverse=True):
        p = latest_by_end[end]
        if (p.basis, p.unit, p.measure) != (newest.basis, newest.unit, newest.measure):
            break
        cohort.append(p)
        if len(cohort) == limit:
            break
    return cohort[::-1]


def node_argument(node, points, policy, observable, issues):
    result = {"status": "missing" if observable else "unobservable", "direction": None,
              "per_period": [], "change_pct": None, "series": {}, "fact_ids": []}
    if not points:
        if observable:
            result["reason"] = sorted(issues) or ["no_eligible_observation"]
        return result
    if not observable:
        raise ValueError(f"unapproved S3 mapping: {node}")
    result.update(status="observed", direction=overall_direction(points, policy),
                  series={p.period.end: p.value for p in points}, fact_ids=[p.fact_id for p in points],
                  frequency=points[0].period.frequency, basis=points[0].basis,
                  unit=points[0].unit, measure=points[0].measure)
    pairs = period_directions(points, policy)
    result["per_period"] = [d for _, d in pairs]
    result["comparison_periods"] = [dates for dates, _ in pairs]
    if len(points) < 2:
        result["reason"] = ["one_observation"]
    elif not policy:
        result["reason"] = ["trend_definition_unconfirmed"]
    elif result["direction"] is None:
        result["reason"] = ["nonconsecutive_or_incomparable_observations"]
    elif policy["method"] in {"absolute", "ordinal", "percentage_points"}:
        result["reason"] = ["change_pct_not_applicable_to_unit"]
        if policy["method"] != "ordinal":
            multiple = 100 if policy["method"] == "percentage_points" else 1
            delta = (Decimal(str(points[-1].value)) - Decimal(str(points[0].value))) * multiple
            result["absolute_change"] = float(delta) if math.isfinite(float(delta)) else None
            result["change_unit"] = "percentage_points" if multiple == 100 else points[0].unit
    else:
        result["change_pct"] = pct_change(points[0].value, points[-1].value)
        if result["change_pct"] is None:
            result["reason"] = ["zero_base_or_nonfinite_change_pct"]
    return result


def interval_direction(points, window, policy):
    if not points or points[0].basis != window[0] or points[0].period.frequency != window[1]:
        return None
    subset = [p for p in points if window[2] <= p.period.end <= window[3]]
    if not subset or (subset[0].period.end, subset[-1].period.end) != window[2:]:
        return None
    return overall_direction(subset, policy)


def expected_status(sign, cause, effect):
    if cause is None or effect is None:
        return "no_data", None
    if cause == "0":
        return "no_change", "0"
    expected = "+" if cause == sign else "-"
    return ("held" if effect == expected else "not_held"), expected


def edge_argument(edge, card, nodes, series, policies):
    cause, effect = edge["cause"], edge["effect"]
    window = None
    cd = ed = None
    if nodes[cause]["direction"] is not None and nodes[effect]["direction"] is not None:
        left, right = series[cause], series[effect]
        if (left[0].basis, left[0].period.frequency) == (right[0].basis, right[0].period.frequency):
            # Match both endpoints of each accounting period, not just year labels.
            common = sorted({(p.period.start, p.period.end) for p in left}
                            & {(p.period.start, p.period.end) for p in right})
            if len(common) >= 2:
                window = (left[0].basis, left[0].period.frequency, common[0][1], common[-1][1])
                cd = interval_direction(left, window, policies[cause])
                ed = interval_direction(right, window, policies[effect])
                if cd is None or ed is None:
                    window = None
    status, expected = expected_status(edge["sign"], cd, ed)
    result = {"status": status, "triggered": [], "unverifiable": []}
    if window:
        result["comparison"] = {"basis": window[0], "frequency": window[1], "from": window[2],
                                "to": window[3], "cause_direction": cd, "effect_direction": ed,
                                "expected_direction": expected}
    for cc in card["counterconditions"]:
        check, required = cc["check_node"], cc["check_direction"]
        actual = nodes.get(check, {}).get("direction")
        if actual is not None and required is not None and window:
            actual = interval_direction(series[check], window, policies[check])
        if actual is None or required is None:
            result["unverifiable"].append(cc["cc_id"])
        elif actual == required:
            result["triggered"].append(cc["cc_id"])
    return result


def build_company(company, as_of, graph, inputs):
    resolved, facts, issues = inputs.resolve(company, as_of)
    config = inputs.config
    policies, nodes, series = {}, {}, {}
    issues_by_node = defaultdict(list)
    for node, reason in issues:
        issues_by_node[node].append(reason)
    for nid, node in graph.nodes.items():
        points = choose_series(resolved[nid].values(), config["lookback_observations"])
        if nid in config.get("node_trends", {}):
            policy = config["node_trends"][nid]
        else:
            policy = config["trend_policies"].get(points[0].unit) if points else None
        policies[nid], series[nid] = policy, points
        observable = node["observable"] or (nid in inputs.confirmed and nid in inputs.overrides)
        nodes[nid] = node_argument(nid, points, policy, observable, issues_by_node[nid])
    edges = {eid: edge_argument(edge, graph.cards[eid], nodes, series, policies)
             for eid, edge in graph.edges.items()}
    used_dates = [p.period.end for seq in series.values() for p in seq]
    period = f"{min(used_dates)}/{max(used_dates)}" if used_dates else None
    arg = {"company_id": company, **inputs.companies[company], "as_of": as_of.isoformat(),
           "period": period, "nodes": nodes, "edges": edges}
    return arg, facts, issues
