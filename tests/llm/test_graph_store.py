"""backend/graph/store.GraphStore: 예전 Neo4j 질의와 같은 규칙으로 경로·확인 조건을 고르는지 본다."""
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend.graph import load_graph as lg  # noqa: E402
from backend.graph.store import GraphStore  # noqa: E402


def node(layer="L0", label=None):
    return {"layer": layer, "label_ko": label}


def edge(cause, effect, sign="+", status="approved"):
    return {"edge_id": f"{cause}->{effect}", "cause": cause, "effect": effect, "sign": sign, "edge_type": "mechanism",
            "empirical_sign": "n/a", "review_status": status, "source_title": "t", "source_class": "team"}


def onehots(ids):
    return {i: np.eye(len(ids))[k] for k, i in enumerate(ids)}


def mixed(vectors, weights):
    """여러 노드와 정해진 cos 유사도를 갖는 질의 벡터 (단위 벡터들의 조합 + 나머지 축)."""
    dim = len(next(iter(vectors.values()))) + 1
    q = np.zeros(dim)
    for i, w in weights.items():
        q[:-1] += w * vectors[i]
    q[-1] = max(0.0, 1 - sum(w * w for w in weights.values())) ** 0.5
    return q


def store_of(nodes, edges, counters=()):
    ids = sorted(nodes)
    emb = {i: np.append(v, 0.0) for i, v in onehots(ids).items()}
    return GraphStore("t1", nodes, edges, list(counters), emb), onehots(ids)


def test_score_is_neo4j_cosine_scale():
    s, vec = store_of({"a": node(label="A"), "b": node(label="B")}, [])
    top = s.top_match(np.append(vec["a"], 0.0))
    assert top == {"node_id": "a", "label": "A", "score": 1.0}
    assert abs(s.scores(np.append(vec["b"], 0.0))[0] - 0.5) < 1e-9          # cos 0 → 0.5


def test_shortest_path_wins_and_lx_endpoints_lose_bonus():
    nodes = {"x": node(label="X"), "m": node(label="M"), "n": node(label="N"), "y": node(label="Y"),
             "lx": node("LX", "외생")}
    edges = [edge("x", "m"), edge("m", "n"), edge("n", "y"), edge("x", "y", "-"), edge("lx", "y")]
    s, vec = store_of(nodes, edges)
    vc = mixed(vec, {"x": 0.9, "lx": 0.9})                                   # 둘 다 같은 유사도
    ve = mixed(vec, {"y": 0.95})
    found = s.find_link(vc, ve, top_k=5)
    assert [n["node_id"] for n in found["nodes"]] == ["x", "y"]              # LX가 아닌 원인, 1단계 직접 경로
    assert [e["sign"] for e in found["edges"]] == ["-"]


def test_min_match_and_hops_limit():
    nodes = {k: node(label=k) for k in "abcdefghij"}
    chain = [edge(a, b) for a, b in zip("abcdefghij", "bcdefghij")]       # a→…→j 9단계
    s, vec = store_of(nodes, chain)
    assert s.find_link(mixed(vec, {"a": 1.0}), mixed(vec, {"j": 1.0})) is None          # 7단계 초과
    assert s.find_link(mixed(vec, {"a": 1.0}), mixed(vec, {"h": 1.0}))["nodes"][-1]["node_id"] == "h"
    assert s.find_link(mixed(vec, {"a": 0.05}), mixed(vec, {"b": 1.0}), min_match=0.55) is None


def test_checks_skip_path_nodes_group_and_order():
    nodes = {"x": node(label="X"), "y": node(label="Y"), "z": node(label="Z"), "w": node(label="가"),
             "v": node("LX", "나")}
    edges = [edge("x", "z"), edge("z", "y")]
    counters = [{"edge_id": "x->z", "check_node": "w", "check_direction": "+"},
                {"edge_id": "z->y", "check_node": "w", "check_direction": "+"},
                {"edge_id": "z->y", "check_node": "v", "check_direction": "-"},
                {"edge_id": "z->y", "check_node": "x", "check_direction": "-"},           # 경로 위 → 빠짐
                {"edge_id": "z->y", "check_node": None, "check_direction": None}]
    s, vec = store_of(nodes, edges, counters)
    found = s.find_link(mixed(vec, {"x": 1.0}), mixed(vec, {"y": 1.0}))
    assert found["checks"] == [
        {"node_id": "w", "label": "가", "layer": "L0", "direction": "+", "edge_ids": ["x->z", "z->y"]},
        {"node_id": "v", "label": "나", "layer": "LX", "direction": "-", "edge_ids": ["z->y"]}]
    assert s.find_link(mixed(vec, {"x": 1.0}), mixed(vec, {"y": 1.0}), max_checks=1)["checks"][0]["node_id"] == "w"


def test_real_graph_raw_material_to_operating_profit():
    graph, cards = lg.load_json()
    variables, edges, counters, _ = lg.build(graph, cards)
    nodes = {v["node_id"]: {"label_ko": v["label_ko"], "layer": v["layer"]} for v in variables}
    ids = sorted(nodes)
    emb = onehots(ids)
    s = GraphStore(graph["graph_version"], nodes, edges, counters, emb)
    found = s.find_link(emb["raw_material_price"], emb["operating_profit"])
    path = [n["node_id"] for n in found["nodes"]]
    assert path[0] == "raw_material_price" and path[-1] == "operating_profit" and len(path) <= 8
    assert all(e["edge_id"] in {x["edge_id"] for x in edges} for e in found["edges"])
    assert len(found["checks"]) <= 8 and not {c["node_id"] for c in found["checks"]} & set(path)


def test_embedding_texts_match_the_neo4j_recipe():
    assert lg.variable_text({"node_id": "gross_margin", "label_ko": "매출총이익률", "synonyms_ko": ["GPM", "총이익률"]}) \
        == "매출총이익률 (GPM, 총이익률) / gross margin"
    assert lg.variable_text({"node_id": "n_abc", "label_ko": "가계 소비", "synonyms_ko": []}) == "가계 소비"
    card = {"sign": "-", "mechanism": "M.", "conditions": ["c1", "c2", "c3"]}
    assert lg.card_text(card, {"label_ko": "A"}, {"label_ko": "B"}) == "A → B (감소). M. 성립 조건: c1 c2 "
