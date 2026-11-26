"""Directed paths over S1 IDs. Signal edges remain indexed for the audit."""
from collections import defaultdict, deque
from heapq import heappop, heappush
from hashlib import sha256
import json


EDGE_TYPES = ("identity", "mechanism", "empirical", "signal")


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def read_json(path):
    def unique(pairs):
        result = dict(pairs)
        require(len(result) == len(pairs), f"duplicate JSON key in {path.name}")
        return result
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique)


def indexed(rows, key):
    result = {}
    for row in rows:
        value = row[key]
        require(isinstance(value, str) and value.strip() and value not in result, f"invalid/duplicate {key}")
        result[value] = row
    return result


class Graph:
    def __init__(self, data):
        self.version = data["graph_version"]
        require(isinstance(self.version, str) and self.version, "missing graph_version")
        self.nodes = indexed(data["nodes"], "node_id")
        self.edges = indexed(data["edges"], "edge_id")
        canonical = {"graph_version": self.version, "nodes": self.nodes, "edges": self.edges}
        self.fingerprint = sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                                              separators=(",", ":")).encode()).hexdigest()
        self.outgoing, self.parents = defaultdict(list), defaultdict(list)
        self.reverse_distances = {}
        for eid, e in sorted(self.edges.items()):
            require(e["cause"] in self.nodes and e["effect"] in self.nodes and e["sign"] in ("+", "-")
                    and e["edge_type"] in EDGE_TYPES, f"invalid graph edge {eid}")
            self.parents[e["effect"]].append(eid)
            if e["edge_type"] != "signal":
                self.outgoing[e["cause"]].append(eid)

    def distances(self, end):
        if end not in self.reverse_distances:
            result, queue = {end: 0}, deque([end])
            while queue:
                node = queue.popleft()
                for eid in self.parents[node]:
                    e = self.edges[eid]
                    parent = e["cause"]
                    if e["edge_type"] != "signal" and parent not in result:
                        result[parent] = result[node] + 1
                        queue.append(parent)
            self.reverse_distances[end] = result
        return self.reverse_distances[end]

    def alternative(self, start, end, max_length):
        """One shortest witness for a mapping hypothesis. Never registers a path use."""
        distances = self.distances(end)
        length = distances.get(start)
        result = {"from_node": start, "to_node": end, "tentative": True, "shortest_length": length}
        if length is None:
            return dict(result, status="no_path")
        if length == 0:
            return dict(result, status="same_node")
        if length > max_length:
            return dict(result, status="search_limited", limit="max_path_length")
        nodes, edges = [start], []
        while nodes[-1] != end:
            current = nodes[-1]
            # Sorted actual edge IDs break ties between shortest witnesses.
            eid = next(e for e in self.outgoing[current]
                       if distances.get(self.edges[e]["effect"]) == distances[current] - 1)
            nodes.append(self.edges[eid]["effect"])
            edges.append(eid)
        return dict(result, status="candidate_only", nodes=nodes, edges=edges)

    def search(self, start, end, explicit, cfg):
        distances = self.distances(end)
        minimum = distances.get(start)
        direct_only = explicit and minimum == 1
        depth = 1 if direct_only else cfg["max_path_length"]
        limit_reasons, candidates = set(), []
        expansions = 0
        # Admissible lower bound on final length, then penalties and stable ID order.
        queue = []
        if minimum and minimum <= depth:
            queue.append((minimum, 0, 0, (), (start,)))
        elif minimum and minimum > depth:
            limit_reasons.add("max_path_length")
        while queue:
            _, empirical, mechanism, edges, nodes = heappop(queue)
            if nodes[-1] == end:
                if len(candidates) == cfg["max_candidates"]:
                    limit_reasons.add("max_candidates")
                    break
                candidates.append({"nodes": list(nodes), "edges": list(edges)})
                continue
            if expansions >= cfg["max_expansions"]:
                limit_reasons.add("max_expansions")
                break
            expansions += 1
            for eid in self.outgoing[nodes[-1]]:
                e = self.edges[eid]
                child = e["effect"]
                if child in nodes or child not in distances:
                    continue
                lower = len(edges) + 1 + distances[child]
                if lower > depth:
                    if not direct_only:
                        limit_reasons.add("max_path_length")
                    continue
                heappush(queue, (lower, empirical + (e["edge_type"] == "empirical"),
                                 mechanism + (e["edge_type"] == "mechanism"), edges + (eid,), nodes + (child,)))
        return candidates, {"scope": "direct_edges" if direct_only else "simple_causal_paths",
                            "shortest_length": minimum, "expansions": expansions,
                            "limits": sorted(limit_reasons), "unexplored_count": None if limit_reasons else 0}


def propagate(direction, sign):
    if sign == "-" and direction in ("+", "-"):
        return "-" if direction == "+" else "+"
    return direction
