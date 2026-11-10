"""Variable 노드와 EdgeCard에 OpenAI 임베딩을 붙이고 벡터 인덱스를 만든다.

load_neo4j.py 다음에 실행한다 (재적재하면 임베딩이 지워지므로 다시 실행).
같은 텍스트는 embeddings_cache.json 에 저장해 두고 다시 호출하지 않는다.

필요한 환경변수: NEO4J_PASSWORD, OPENAI_API_KEY (저장소 맨 위 .env)
  python backend/graph/embed_neo4j.py
임베딩 모델은 backend/llm/graph_props.py(OPENAI_EMBED_MODEL)와 같아야 한다.
"""
import hashlib
import json
import os
from pathlib import Path

from neo4j import GraphDatabase
from openai import OpenAI

MODEL = os.getenv("OPENAI_EMBED_MODEL") or "text-embedding-3-small"
DIM = 1536
REPO = Path(__file__).resolve().parents[2]
CACHE = Path(__file__).resolve().parent / "embeddings_cache.json"   # Git에 올리지 않음 (약 40MB)

VAR_Q = """
MATCH (n:Variable)
RETURN n.node_id AS id,
       n.label_ko + CASE WHEN size(n.synonyms_ko) > 0
                         THEN ' (' + reduce(t = '', x IN n.synonyms_ko | t + CASE WHEN t = '' THEN '' ELSE ', ' END + x) + ')'
                         ELSE '' END
       // 설계 노드는 영어 ID(gross_margin 등)에도 뜻이 있어서 함께 넣는다. n_ 로 시작하는 ID는 해시라 뺀다.
       + CASE WHEN n.node_id STARTS WITH 'n_' THEN '' ELSE ' / ' + replace(n.node_id, '_', ' ') END AS text
"""
CARD_Q = """
MATCH (c:EdgeCard)-[:CAUSE]->(a), (c)-[:EFFECT]->(b)
RETURN c.edge_id AS id,
       a.label_ko + ' → ' + b.label_ko + ' (' + CASE c.sign WHEN '+' THEN '증가' ELSE '감소' END + '). '
       + c.mechanism + ' 성립 조건: ' + reduce(t = '', x IN c.conditions[0..2] | t + x + ' ') AS text
"""


def load_env():
    """저장소 맨 위 .env (NEO4J_PASSWORD, OPENAI_API_KEY 등). 이미 있는 환경변수는 덮어쓰지 않는다."""
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO / ".env")
    except ImportError:
        pass


def key(text):
    return hashlib.sha1((MODEL + "\x1f" + text).encode("utf-8")).hexdigest()


def embed_all(client, texts, cache):
    todo = sorted({t for t in texts if key(t) not in cache})
    for i in range(0, len(todo), 200):
        batch = todo[i:i + 200]
        resp = client.embeddings.create(model=MODEL, input=batch)
        for t, d in zip(batch, resp.data):
            cache[key(t)] = d.embedding
    return len(todo)


def main():
    load_env()
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    client = OpenAI()  # OPENAI_API_KEY 환경변수 사용
    driver = GraphDatabase.driver(os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
                                  auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]))
    with driver, driver.session() as s:
        jobs = [("Variable", "node_id", VAR_Q), ("EdgeCard", "edge_id", CARD_Q)]
        rows = {label: s.run(q).data() for label, _, q in jobs}
        n_new = embed_all(client, [r["text"] for rs in rows.values() for r in rs], cache)
        CACHE.write_text(json.dumps(cache), encoding="utf-8")
        print("new embeddings", n_new)

        for label, idkey, _ in jobs:
            data = [{"id": r["id"], "text": r["text"], "v": cache[key(r["text"])]} for r in rows[label]]
            for i in range(0, len(data), 200):
                s.run(f"""
                    UNWIND $rows AS r
                    MATCH (n:{label} {{{idkey}: r.id}})
                    SET n.embedding_text = r.text
                    WITH n, r CALL db.create.setNodeVectorProperty(n, 'embedding', r.v)
                    """, rows=data[i:i + 200]).consume()
            s.run(f"CREATE VECTOR INDEX {label.lower()}_embedding IF NOT EXISTS FOR (n:{label}) ON n.embedding "
                  f"OPTIONS {{indexConfig: {{`vector.dimensions`: {DIM}, `vector.similarity_function`: 'cosine'}}}}"
                  ).consume()
            n = s.run(f"MATCH (n:{label}) WHERE n.embedding IS NOT NULL RETURN count(n)").single()[0]
            print(label, "embedded", n)
        s.run("CALL db.awaitIndexes(300)").consume()
    print("OK")


if __name__ == "__main__":
    main()
