"""data/fds_graph(graph.json + edge_cards/)를 Supabase 표로 적재하고 임베딩을 붙인다.

    python backend/graph/load_graph.py --dry-run   # DB·OpenAI 없이 변환·개수만 확인
    python backend/graph/load_graph.py             # .env의 SUPABASE_DB_URL, OPENAI_API_KEY 사용

표가 없으면 data_pipeline/migrations/002_fds_graph.sql을 먼저 적용한다. 실행할 때마다 표를 통째로 바꾼다.
임베딩 텍스트는 예전 Neo4j 적재(embed_neo4j.py, 삭제됨)와 같다. 같은 텍스트는 embeddings_cache.json에 남겨
다시 호출하지 않는다. 임베딩 모델은 backend/llm/graph_props.py(OPENAI_EMBED_MODEL)와 같아야 한다.
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "data" / "fds_graph"
CACHE = Path(__file__).resolve().parent / "embeddings_cache.json"   # Git 제외
MIGRATION = REPO / "data_pipeline" / "migrations" / "002_fds_graph.sql"
VALUE_NODES = {"firm_value", "equity_value", "per_share_value", "reported_firm_value", "firm_valuation",
               "firm_tobins_q", "market_equity", "market_firm_value", "shareholder_value", "startup_value"}


def embed_model():
    return os.getenv("OPENAI_EMBED_MODEL") or "text-embedding-3-small"


def load_json():
    graph = json.loads((ROOT / "graph.json").read_text(encoding="utf-8"))
    cards = {}
    for f in sorted((ROOT / "edge_cards").glob("*.json")):
        c = json.loads(f.read_text(encoding="utf-8"))
        cards[c["edge_id"]] = c
    edge_ids = {e["edge_id"] for e in graph["edges"]}
    if edge_ids != set(cards):
        sys.exit(f"graph edges와 카드가 일치하지 않음: {len(edge_ids ^ set(cards))}건")
    for e in graph["edges"]:
        if cards[e["edge_id"]]["sign"] != e["sign"]:
            sys.exit(f"sign 불일치: {e['edge_id']}")
    return graph, cards


def variable_text(n):
    """embed_neo4j.py VAR_Q와 같은 문장: 이름 (동의어, …) / 영어 ID(해시 ID 'n_'는 뺌)."""
    text = n["label_ko"]
    if n.get("synonyms_ko"):
        text += " (" + ", ".join(n["synonyms_ko"]) + ")"
    if not n["node_id"].startswith("n_"):
        text += " / " + n["node_id"].replace("_", " ")
    return text


def card_text(card, cause, effect):
    """embed_neo4j.py CARD_Q와 같은 문장."""
    head = f"{cause['label_ko']} → {effect['label_ko']} ({'증가' if card['sign'] == '+' else '감소'}). "
    return head + card["mechanism"] + " 성립 조건: " + "".join(x + " " for x in card["conditions"][:2])


def build(graph, cards):
    nodes = {n["node_id"]: n for n in graph["nodes"]}
    variables = [{**{k: n.get(k) for k in ("node_id", "layer", "label_ko", "observable", "source_variable",
                                            "unit", "can_be_claim")},
                  "synonyms_ko": n.get("synonyms_ko") or [], "is_value": n["node_id"] in VALUE_NODES}
                 for n in graph["nodes"]]
    edges, counters, texts = [], [], []
    for e in graph["edges"]:
        c = cards[e["edge_id"]]
        edges.append({"edge_id": e["edge_id"], "cause": e["cause"], "effect": e["effect"], "sign": e["sign"],
                      "edge_type": e.get("edge_type"), "empirical_sign": c["empirical_sign"],
                      "review_status": c["review_status"], "mechanism": c["mechanism"],
                      "conditions": c["conditions"], "source_title": c["source"]["title"],
                      "source_author": c["source"]["author"], "source_class": c["source"]["source_class"]})
        for cc in c["counterconditions"]:
            counters.append({"cc_id": cc["cc_id"], "edge_id": e["edge_id"], "text": cc["text"],
                             "check_node": cc.get("check_node"), "check_direction": cc.get("check_direction")})
        texts.append(("card", e["edge_id"], card_text(c, nodes[e["cause"]], nodes[e["effect"]])))
    texts = [("variable", n["node_id"], variable_text(n)) for n in graph["nodes"]] + texts
    return variables, edges, counters, texts


def cache_key(model, text):
    return hashlib.sha1((model + "\x1f" + text).encode("utf-8")).hexdigest()


def embed_all(texts, model):
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    todo = sorted({t for _, _, t in texts if cache_key(model, t) not in cache})
    if todo:
        from openai import OpenAI
        client = OpenAI()
        for i in range(0, len(todo), 200):
            batch = todo[i:i + 200]
            resp = client.embeddings.create(model=model, input=batch)
            for t, d in zip(batch, resp.data):
                cache[cache_key(model, t)] = d.embedding
        CACHE.write_text(json.dumps(cache), encoding="utf-8")
    print(f"embeddings: {len(texts)} texts, {len(todo)} new from OpenAI ({model})")
    return [{"kind": k, "id": i, "model": model, "text": t, "vector": cache[cache_key(model, t)]}
            for k, i, t in texts]


def write(url, graph, variables, edges, counters, embeddings):
    from sqlalchemy import create_engine, text

    engine = create_engine(url, hide_parameters=True)
    with engine.begin() as c:
        exists = c.scalar(text("SELECT to_regclass('public.fds_variables') IS NOT NULL"))
        if not exists:
            c.exec_driver_sql(MIGRATION.read_text(encoding="utf-8").replace("BEGIN;", "").replace("COMMIT;", ""))
        c.execute(text("TRUNCATE fds_counterconditions, fds_edges, fds_variables, fds_embeddings, fds_graph_meta"))
        def insert(table, rows):
            if rows:
                cols = list(rows[0])
                c.execute(text(f"INSERT INTO {table} ({', '.join(cols)}) "
                               f"SELECT {', '.join(cols)} FROM jsonb_populate_recordset(NULL::{table}, CAST(:rows AS jsonb))"),
                          {"rows": json.dumps(rows, ensure_ascii=False)})
        insert("fds_variables", variables)
        insert("fds_edges", edges)
        insert("fds_counterconditions", counters)
        for i in range(0, len(embeddings), 200):
            insert("fds_embeddings", embeddings[i:i + 200])
        c.execute(text("INSERT INTO fds_graph_meta (graph_version, variables, edges) VALUES (:v, :n, :e)"),
                  {"v": graph["graph_version"], "n": len(variables), "e": len(edges)})
        counts = {t: c.scalar(text(f"SELECT count(*) FROM {t}")) for t in
                  ("fds_variables", "fds_edges", "fds_counterconditions", "fds_embeddings")}
    engine.dispose()
    return counts


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO / ".env")
    except ImportError:
        pass
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    graph, cards = load_json()
    variables, edges, counters, texts = build(graph, cards)
    checks = sum(1 for c in counters if c["check_node"])
    print(f"graph {graph['graph_version']}: variables {len(variables)}, edges {len(edges)}, "
          f"counterconditions {len(counters)} (with a variable to check: {checks}), "
          f"sources {len({(e['source_title'], e['source_author'], e['source_class']) for e in edges})}")
    if args.dry_run:
        print("dry run: nothing embedded or written")
        return
    embeddings = embed_all(texts, embed_model())
    counts = write(os.environ["SUPABASE_DB_URL"].strip(), graph, variables, edges, counters, embeddings)
    expected = {"fds_variables": len(variables), "fds_edges": len(edges),
                "fds_counterconditions": len(counters), "fds_embeddings": len(embeddings)}
    print("loaded:", counts)
    if counts != expected:
        sys.exit(f"count mismatch: expected {expected}")


if __name__ == "__main__":
    main()
