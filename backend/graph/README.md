# backend/graph — FDS 인과그래프 Neo4j

`data/fds_graph/`의 `graph.json`과 `edge_cards/*.json`이 원본이다. Neo4j는 조회용 사본이며, 적재할 때마다 FDS 데이터를 지우고 다시 넣는다. 분석 단계의 경제명제(`backend/llm/graph_props.py`)가 이 DB를 읽는다.

## 실행 (저장소 맨 위에서)

1. `.env`에 `NEO4J_PASSWORD=<8자 이상>`과 `OPENAI_API_KEY`를 넣는다. 이 파일은 Git에 올리지 않는다.
2. Neo4j를 띄운다 (APOC·GenAI 플러그인 포함):
   ```bash
   docker compose --env-file .env -f backend/graph/docker-compose.yml up -d
   ```
3. 적재하고 임베딩을 붙인다:
   ```bash
   pip install neo4j openai python-dotenv
   python backend/graph/load_neo4j.py --dry-run
   python backend/graph/load_neo4j.py
   python backend/graph/embed_neo4j.py
   ```
4. http://localhost:7474 (Neo4j Browser)에서 `queries.cypher`의 쿼리를 실행해 볼 수 있다.

기대 개수: Variable 563, CAUSES 641, EdgeCard 641, CounterCondition 9,524, CHECKS 9,522, Source 529, Value 10. 적재가 끝나면 실제 개수와 비교하고, 다르면 실패로 끝난다.

`load_neo4j.py`로 다시 적재하면 임베딩이 지워진다. 그 뒤에 `embed_neo4j.py`를 다시 돌린다. 같은 텍스트의 임베딩은 `embeddings_cache.json`(Git 제외, 약 40MB)에 남아 있어서 OpenAI를 다시 부르지 않는다.

## 스키마

```
(:Variable:<Layer>[:Claim][:Value])-[:CAUSES {edge_id, sign, edge_type, empirical_sign, review_status}]->(:Variable)
(:EdgeCard {edge_id, sign, empirical_sign, mechanism, conditions[], review_status})
   -[:CAUSE]->(:Variable)   -[:EFFECT]->(:Variable)
   -[:HAS_COUNTER]->(:CounterCondition {cc_id, text, check_direction})-[:CHECKS]->(:Variable)
   -[:CITES]->(:Source {source_id, title, author, source_class})
(:GraphMeta {graph_version, loaded_at})
```

- `<Layer>`는 LX, L0~L4 중 하나다. `can_be_claim=true`인 노드에는 `:Claim`, 원본 뷰어가 금색으로 그리던 가치 노드 10개에는 `:Value` 라벨이 붙는다.
- 카드의 counterconditions와 source는 객체라서 Neo4j 속성에 넣을 수 없다. 그래서 별도 노드로 만든다. `check_node=null`인 맥락 조건 2개는 `:CHECKS` 관계 없이 남는다.
- 모든 노드에 `name` 속성이 있어 Browser가 이름을 자동으로 표시한다.

## 벡터 검색

- `embed_neo4j.py`: Variable 563개(한글 이름 + 동의어 + 설계 노드의 영어 ID)와 EdgeCard 641개(원인 → 결과, 메커니즘, 성립 조건 앞 2개)를 `text-embedding-3-small`(1536차원)로 임베딩한다. 벡터 인덱스는 `variable_embedding`, `edgecard_embedding`이다.
- 변수 이름, 동의어, 카드 텍스트가 OpenAI로 전송된다.
- `OPENAI_EMBED_MODEL`을 바꾸면 그래프 임베딩과 `graph_props.py`가 같은 모델을 써야 한다.

## Browser 쿼리 (`queries.cypher`)

| 쿼리 | 내용 |
|---|---|
| H1~H5 | 원본 HTML 뷰어의 가치 중심 / 재무 연결 / 전체 / 노드 목록 / 관계 목록과 같은 결과 |
| V1~V3 | H1~H3을 부호별 색으로 (가상 노드·관계, APOC). 노드 라벨 Value / LX / Variable, 관계 POS / NEG / SIGNAL_POS / SIGNAL_NEG |
| P0, P1 | 관계 찾기, 카드 하나를 그래프로 펼치기 (원인·결과·메커니즘·성립조건·깨지는 조건·출처) |
| 1~9 | 경로·누적 부호, 반박할 때 확인할 변수 등 예시 |
| D1, D2 | 구절 → 벡터 검색 → 경로 → 확인할 변수 (GenAI 플러그인). 실행 전에 `:param {openai_key: '...'}` |

Browser 색은 결과 프레임 오른쪽 Results overview의 칩을 눌러 한 번 지정한다 (원본 뷰어 색: Value `#ad8230`, LX `#8ca193`, Variable `#2d715a`, POS `#27745d`, NEG `#bf5948`). Browser는 점선을 지원하지 않아 signal은 연한 색으로 구분한다.

D1·D2에 넣은 OpenAI 키는 Browser 설정의 "Retain parameters in local storage"가 켜져 있으면 브라우저에 남는다. 쓴 뒤 `:param {openai_key: ''}`로 지운다.

## 한계

- 카드 641개가 모두 검토 전 초안(draft)이다.
- `CALL (변수) { ... }` 문법 때문에 Neo4j 5.23 이상이 필요하다. `docker-compose.yml`은 5.26을 쓴다.
- 다른 Neo4j가 이미 7474·7687 포트를 쓰고 있으면 이 compose는 뜨지 않는다.
