# backend/graph — FDS 공통 인과그래프

원본은 `data/fds_graph/`(`graph.json` + `edge_cards/*.json`, graph_version g1.1)이고, Supabase 표는 조회용 사본이다.
LLM 분석의 경제명제(`backend/llm/graph_props.py`, 근거 `PR-n`)가 이 그래프를 쓴다.

| 파일 | 역할 |
|---|---|
| `load_graph.py` | JSON → Supabase 표(`fds_*`) 적재 + OpenAI 임베딩. 실행할 때마다 표를 통째로 바꾼다 |
| `store.py` | 표를 한 번 읽어 메모리에서 찾는다: 구절과 가까운 변수, 원인→결과 경로, 깨지는 조건 |
| `../../data_pipeline/migrations/002_fds_graph.sql` | 표 정의 (`load_graph.py`가 없으면 만든다) |

## 적재 (그래프 JSON이 바뀔 때만)

```bash
python backend/graph/load_graph.py --dry-run   # 개수만: 변수 563, 관계 641, 깨지는 조건 9,524, 출처 529
python backend/graph/load_graph.py             # .env: SUPABASE_DB_URL, OPENAI_API_KEY
```

임베딩은 변수 563개 + 카드 641개를 `text-embedding-3-small`(1536차원)로 만든다. 같은 텍스트는
`backend/graph/embeddings_cache.json`(Git 제외)에 남겨 다시 부르지 않는다. 변수 이름·동의어·카드 문장이 OpenAI로 전송된다.
`OPENAI_EMBED_MODEL`을 바꾸면 적재와 `graph_props.py`가 같은 모델을 써야 한다.

## 찾는 규칙 (예전 Neo4j 질의와 같음)

- 유사도는 Neo4j 벡터 인덱스(cosine)와 같은 `(1 + cos) / 2`. 0.55 미만은 후보가 아니고, 0.8 미만 짝짓기는 근거 문장에 '틀릴 수 있음'을 붙인다.
- 원인·결과 구절마다 후보 30개, 1~7단계 방향 경로 중 점수(유사도 합 − 0.01×길이 + 양 끝이 외생 변수(LX)가 아니면 0.03씩)가 가장 높은 것.
- 확인 조건: 경로 위 카드의 깨지는 조건 중 경로 밖 변수, (변수, 방향)별로 묶어 LX가 아닌 것·관계가 많은 것·이름 순으로 8개.

## 한계

- g1.1 카드 641개는 팀 결정(2026-11-11)으로 모두 `approved`다. 실증 카드의 메커니즘 문장은 LLM 초안에서 출발했다(`data/fds_graph/README.md`).
- 구절과 변수를 임베딩 유사도로 짝짓기 때문에 엉뚱한 변수에 붙을 수 있다. 근거 문장과 `values.match`에 유사도를 남긴다.
- 그래프는 이론상 경로와 조건이다. 회사가 실제로 그렇게 됐는지는 모델 근거(FS·OH·DP)로 본다.
