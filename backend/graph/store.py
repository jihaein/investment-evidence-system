"""FDS 공통 인과그래프 조회 (Supabase 표 → 메모리).

그래프는 변수 563개·관계 641개로 작아서, 표를 한 번 읽어 메모리에서 찾는다. 예전 Neo4j 질의
(backend/llm/graph_props.py의 PATH_QUERY·TOP1_QUERY)와 같은 결과를 낸다:
  - 유사도: Neo4j 벡터 인덱스(cosine)와 같은 (1 + cos) / 2. MIN_MATCH 0.55·CLEAR_MATCH 0.8이 이 눈금 기준이다
  - 후보: 원인·결과 구절마다 상위 top_k 노드, 각각 min_match 이상, 서로 다른 노드
  - 경로: 원인 후보 → 결과 후보, 1~max_hops 단계의 방향 경로. 점수 = 유사도 둘의 합 − 0.01 × 길이
          + 양 끝이 LX(외생 변수)가 아니면 각각 0.03. 가장 높은 것 하나 (같은 점수면 유사도 높은 후보, edge_id 순)
  - 확인 조건: 경로 위 관계 카드의 깨지는 조건 중 경로 밖 변수. (변수, 방향)마다 관계를 모으고
          LX가 아닌 것, 관계가 많은 것, 이름 순으로 max_checks개
"""
from collections import deque

import numpy as np


class GraphStore:
    def __init__(self, version, nodes, edges, counters, embeddings):
        """nodes: {node_id: {label_ko, layer, ...}}, edges: [{edge_id, cause, effect, sign, edge_type,
        empirical_sign, review_status, source_title, source_class}], counters: [{edge_id, check_node,
        check_direction}], embeddings: {node_id: vector}."""
        self.version = version
        self.nodes = nodes
        self.edges = {e["edge_id"]: e for e in edges}
        self.out = {}
        for e in sorted(edges, key=lambda e: e["edge_id"]):
            self.out.setdefault(e["cause"], []).append(e)
        self.counters = {}
        for c in counters:
            self.counters.setdefault(c["edge_id"], []).append(c)
        ids = sorted(i for i in embeddings if i in nodes)
        if not ids:
            raise ValueError("그래프 임베딩이 없음 (backend/graph/load_graph.py로 적재)")
        matrix = np.asarray([embeddings[i] for i in ids], dtype=np.float64)
        self.ids = ids
        self.matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)

    # ------------------------------------------------------------------ 유사도
    def scores(self, vector):
        v = np.asarray(vector, dtype=np.float64)
        v = v / np.linalg.norm(v)
        return (1.0 + self.matrix @ v) / 2.0

    def top(self, vector, k):
        s = self.scores(vector)
        order = sorted(range(len(s)), key=lambda i: (-s[i], self.ids[i]))[:k]
        return [(self.ids[i], float(s[i])) for i in order]

    def top_match(self, vector):
        (node_id, score), = self.top(vector, 1)
        return {"node_id": node_id, "label": self.nodes[node_id]["label_ko"], "score": round(score, 3)}

    # ------------------------------------------------------------------ 경로
    def _shortest(self, start, max_hops):
        """start에서 닿는 노드마다 가장 짧은 경로(관계 목록). 같은 길이면 edge_id 순으로 먼저 찾은 것."""
        paths = {start: []}
        queue = deque([start])
        while queue:
            node = queue.popleft()
            if len(paths[node]) == max_hops:
                continue
            for e in self.out.get(node, []):
                if e["effect"] not in paths:
                    paths[e["effect"]] = paths[node] + [e]
                    queue.append(e["effect"])
        return paths

    def _lx(self, node_id):
        return self.nodes[node_id]["layer"] == "LX"

    def find_link(self, vc, ve, top_k=30, min_match=0.55, max_hops=7, max_checks=8):
        causes = [(n, s) for n, s in self.top(vc, top_k) if s >= min_match]
        effects = [(n, s) for n, s in self.top(ve, top_k) if s >= min_match]
        best = None
        for ca, sa in causes:
            reach = self._shortest(ca, max_hops)
            for ef, sb in effects:
                if ef == ca or ef not in reach:
                    continue
                path = reach[ef]
                score = (sa + sb - 0.01 * len(path) + (0 if self._lx(ca) else 0.03)
                         + (0 if self._lx(ef) else 0.03))
                key = (round(score, 12), sa, sb)
                if best is None or key > best[0]:
                    best = (key, ca, ef, sa, sb, path)
        if best is None:
            return None
        _, ca, _, sa, sb, path = best
        node_ids = [ca] + [e["effect"] for e in path]
        return {
            "nodes": [self._node(n) for n in node_ids],
            "edges": [{k: e[k] for k in ("edge_id", "sign", "edge_type", "empirical_sign", "review_status",
                                         "source_title", "source_class")} for e in path],
            "checks": self._checks(path, set(node_ids), max_checks),
            "cause_match": sa, "effect_match": sb,
        }

    def _node(self, node_id):
        n = self.nodes[node_id]
        return {"node_id": node_id, "label": n["label_ko"], "layer": n["layer"]}

    def _checks(self, path, on_path, limit):
        groups = {}
        for e in path:
            for c in self.counters.get(e["edge_id"], []):
                v = c.get("check_node")
                if not v or v in on_path or v not in self.nodes:
                    continue
                ids = groups.setdefault((v, c["check_direction"]), [])
                if e["edge_id"] not in ids:
                    ids.append(e["edge_id"])
        ordered = sorted(groups.items(), key=lambda kv: (self._lx(kv[0][0]), -len(kv[1]),
                                                         self.nodes[kv[0][0]]["label_ko"]))
        return [{**self._node(v), "direction": d, "edge_ids": ids} for (v, d), ids in ordered[:limit]]


# ---------------------------------------------------------------------- 읽어 오기

def load_from_db(url, model):
    """Supabase(Postgres)의 fds_* 표에서 그래프와 임베딩을 읽는다."""
    from sqlalchemy import create_engine, text

    engine = create_engine(url, hide_parameters=True)
    with engine.connect() as c:
        version = c.scalar(text("SELECT graph_version FROM fds_graph_meta ORDER BY loaded_at DESC LIMIT 1"))
        nodes = {r.node_id: {"label_ko": r.label_ko, "layer": r.layer}
                 for r in c.execute(text("SELECT node_id, label_ko, layer FROM fds_variables"))}
        edges = [dict(r._mapping) for r in c.execute(text(
            "SELECT edge_id, cause, effect, sign, edge_type, empirical_sign, review_status, "
            "source_title, source_class FROM fds_edges"))]
        counters = [dict(r._mapping) for r in c.execute(text(
            "SELECT edge_id, check_node, check_direction FROM fds_counterconditions"))]
        embeddings = {r.id: r.vector for r in c.execute(text(
            "SELECT id, vector FROM fds_embeddings WHERE kind = 'variable' AND model = :m"), {"m": model})}
    engine.dispose()
    if not version:
        raise ValueError("그래프가 적재되지 않음 (backend/graph/load_graph.py)")
    return GraphStore(version, nodes, edges, counters, embeddings)
