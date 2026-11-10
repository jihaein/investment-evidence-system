"""data/fds_graph의 graph.json + edge_cards/*.json 을 Neo4j로 적재한다.

JSON이 원본이고 Neo4j는 조회용 사본이다. 실행할 때마다 FDS 데이터를 지우고 다시 넣는다.

스키마
  (:Variable:<Layer>[:Claim])-[:CAUSES {edge_id, sign, edge_type, empirical_sign, review_status}]->(:Variable)
  (:EdgeCard)-[:CAUSE]->(:Variable), (:EdgeCard)-[:EFFECT]->(:Variable)
  (:EdgeCard)-[:HAS_COUNTER]->(:CounterCondition)-[:CHECKS]->(:Variable)
  (:EdgeCard)-[:CITES]->(:Source)
  (:GraphMeta {graph_version})

사용
  python backend/graph/load_neo4j.py --dry-run   # DB 없이 변환·개수만 확인
  python backend/graph/load_neo4j.py             # .env의 NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD 사용
그 다음 python backend/graph/embed_neo4j.py 로 임베딩을 다시 붙인다 (재적재하면 지워짐).
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]                   # 저장소 맨 위
ROOT = REPO / "data" / "fds_graph"                          # graph.json, edge_cards/
LAYERS = ["LX", "L0", "L1", "L2", "L3", "L4"]
# HTML 뷰어(viewer.html)의 targets: 금색으로 그리는 가치 노드
VALUE_NODES = ["firm_value", "equity_value", "per_share_value", "reported_firm_value", "firm_valuation",
               "firm_tobins_q", "market_equity", "market_firm_value", "shareholder_value", "startup_value"]
FDS_LABELS = ["Variable", "EdgeCard", "CounterCondition", "Source", "GraphMeta"]
BATCH = 500


def load_env():
    """저장소 맨 위 .env (NEO4J_PASSWORD, OPENAI_API_KEY 등). 이미 있는 환경변수는 덮어쓰지 않는다."""
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO / ".env")
    except ImportError:
        pass


def load_json():
    graph = json.loads((ROOT / "graph.json").read_text(encoding="utf-8"))
    cards = {}
    for f in sorted((ROOT / "edge_cards").glob("*.json")):
        c = json.loads(f.read_text(encoding="utf-8"))
        cards[c["edge_id"]] = c
    return graph, cards


def source_id(src):
    key = "\x1f".join([src["title"], src["author"], src["source_class"]])
    return "src_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def build_rows(graph, cards):
    edge_ids = {e["edge_id"] for e in graph["edges"]}
    if edge_ids != set(cards):
        sys.exit(f"graph edges와 카드가 일치하지 않음: {len(edge_ids ^ set(cards))}건")

    variables = [dict(n) for n in graph["nodes"]]
    edges, card_rows, cc_rows, sources = [], [], [], {}
    for e in graph["edges"]:
        c = cards[e["edge_id"]]
        if c["sign"] != e["sign"]:
            sys.exit(f"sign 불일치: {e['edge_id']}")
        edges.append({**e, "empirical_sign": c["empirical_sign"], "review_status": c["review_status"]})
        sid = source_id(c["source"])
        sources[sid] = {"source_id": sid, **c["source"]}
        card_rows.append({
            "edge_id": c["edge_id"], "cause": e["cause"], "effect": e["effect"],
            "sign": c["sign"], "empirical_sign": c["empirical_sign"], "mechanism": c["mechanism"],
            "conditions": c["conditions"], "review_status": c["review_status"], "source_id": sid,
        })
        for cc in c["counterconditions"]:
            cc_rows.append({**cc, "edge_id": c["edge_id"]})
    return variables, edges, card_rows, cc_rows, list(sources.values())


def batches(rows):
    for i in range(0, len(rows), BATCH):
        yield rows[i:i + BATCH]


def run(session, query, rows):
    for b in batches(rows):
        session.run(query, rows=b).consume()


def load(driver, database, graph, variables, edges, card_rows, cc_rows, sources):
    with driver.session(database=database) as s:
        for label in FDS_LABELS:
            s.run(f"MATCH (n:{label}) DETACH DELETE n").consume()  # 1만여 개 규모라 한 번에 삭제

        for label, key in [("Variable", "node_id"), ("EdgeCard", "edge_id"),
                           ("CounterCondition", "cc_id"), ("Source", "source_id")]:
            s.run(f"CREATE CONSTRAINT {label.lower()}_{key} IF NOT EXISTS "
                  f"FOR (n:{label}) REQUIRE n.{key} IS UNIQUE").consume()
        s.run("CREATE INDEX causes_edge_id IF NOT EXISTS FOR ()-[r:CAUSES]-() ON (r.edge_id)").consume()
        s.run("CREATE FULLTEXT INDEX variable_label_ko IF NOT EXISTS "
              "FOR (n:Variable) ON EACH [n.label_ko, n.synonyms_text]").consume()

        s.run("CREATE (:GraphMeta {graph_version: $v, loaded_at: datetime()})",
              v=graph["graph_version"]).consume()

        for layer in LAYERS:  # 라벨은 고정 목록에서만 쿼리에 넣는다
            rows = [v for v in variables if v["layer"] == layer]
            run(s, f"""
                UNWIND $rows AS r
                CREATE (n:Variable:{layer})
                SET n = r, n.name = r.label_ko,
                    n.synonyms_text = reduce(t = '', x IN r.synonyms_ko | t + ' ' + x)
                """, rows)
        s.run("MATCH (n:Variable {can_be_claim: true}) SET n:Claim").consume()
        s.run("MATCH (n:Variable) WHERE n.node_id IN $ids SET n:Value", ids=VALUE_NODES).consume()

        run(s, """
            UNWIND $rows AS r
            MATCH (a:Variable {node_id: r.cause}), (b:Variable {node_id: r.effect})
            CREATE (a)-[:CAUSES {edge_id: r.edge_id, sign: r.sign, edge_type: r.edge_type,
                                 empirical_sign: r.empirical_sign, review_status: r.review_status}]->(b)
            """, edges)

        run(s, "UNWIND $rows AS r CREATE (:Source {source_id: r.source_id, name: r.title, title: r.title, "
               "author: r.author, source_class: r.source_class})", sources)

        run(s, """
            UNWIND $rows AS r
            MATCH (a:Variable {node_id: r.cause}), (b:Variable {node_id: r.effect}),
                  (src:Source {source_id: r.source_id})
            CREATE (c:EdgeCard {edge_id: r.edge_id, name: r.edge_id, sign: r.sign, empirical_sign: r.empirical_sign,
                                mechanism: r.mechanism, conditions: r.conditions,
                                review_status: r.review_status})
            CREATE (c)-[:CAUSE]->(a), (c)-[:EFFECT]->(b), (c)-[:CITES]->(src)
            """, card_rows)

        run(s, """
            UNWIND $rows AS r
            MATCH (c:EdgeCard {edge_id: r.edge_id})
            CREATE (c)-[:HAS_COUNTER]->(cc:CounterCondition {cc_id: r.cc_id, name: r.text, text: r.text,
                                                            check_direction: r.check_direction})
            WITH cc, r WHERE r.check_node IS NOT NULL
            MATCH (v:Variable {node_id: r.check_node})
            CREATE (cc)-[:CHECKS]->(v)
            """, cc_rows)

        counts = {
            "Variable": "MATCH (n:Variable) RETURN count(n)",
            "CAUSES": "MATCH ()-[r:CAUSES]->() RETURN count(r)",
            "EdgeCard": "MATCH (n:EdgeCard) RETURN count(n)",
            "CounterCondition": "MATCH (n:CounterCondition) RETURN count(n)",
            "CHECKS": "MATCH ()-[r:CHECKS]->() RETURN count(r)",
            "Source": "MATCH (n:Source) RETURN count(n)",
            "Value": "MATCH (n:Value) RETURN count(n)",
        }
        return {k: s.run(q).single()[0] for k, q in counts.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="DB에 연결하지 않고 변환 결과만 확인")
    ap.add_argument("--database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))
    args = ap.parse_args()
    load_env()

    graph, cards = load_json()
    variables, edges, card_rows, cc_rows, sources = build_rows(graph, cards)
    expected = {
        "Variable": len(variables), "CAUSES": len(edges), "EdgeCard": len(card_rows),
        "CounterCondition": len(cc_rows),
        "CHECKS": sum(1 for r in cc_rows if r["check_node"] is not None),
        "Source": len(sources),
        "Value": sum(1 for v in variables if v["node_id"] in VALUE_NODES),
    }
    print("graph_version", graph["graph_version"])
    print("expected", expected)
    if args.dry_run:
        return

    from neo4j import GraphDatabase
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    auth = (os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"])
    with GraphDatabase.driver(uri, auth=auth) as driver:
        driver.verify_connectivity()
        actual = load(driver, args.database, graph, variables, edges, card_rows, cc_rows, sources)
    print("loaded  ", actual)
    if actual != expected:
        sys.exit("개수 불일치")
    print("OK")


if __name__ == "__main__":
    main()
