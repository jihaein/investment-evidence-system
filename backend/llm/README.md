# backend/llm — LLM 연결

투자자 의견을 모델 결과(근거)로 검증하고, 캐릭터가 각자 말투로 설명하는 부분
원칙은 **사실은 한 번만 정하고(분석 단계), 말투만 캐릭터마다 바꾼다** 임

## 흐름

1. `build_pack.py` — 입력 1~4를 근거 묶음(JSON) 하나로 만든다. **LLM을 쓰지 않는다** (지금 구현됨)
2. 분석 단계 — 투자자 의견을 주장으로 나누고, 주장마다 근거(ID)로 지지·반박·근거 없음을 정한다 (예정)
3. 캐릭터 6개 — 분석 결과를 각자 말투로 설명한다. 사실은 바꾸지 않는다 (예정)
4. 검증 — 답에 나온 근거 ID·숫자·금지 표현을 코드로 확인한다 (예정)

## 근거 묶음 (`build_pack.py`)

```bash
python backend/llm/build_pack.py 010170 2016-06-30 2019-06-28 --opinion "영업이익률이 좋아질 것"
python backend/llm/build_pack.py 010170 2016-06-30 2019-06-28 --opinion-file 의견.txt --out pack.json
```

- 근거마다 ID가 붙는다: F-Score `FS-n`, Ohlson `OH-n`, DuPont `DP-n`, 임계 모델 `GT-n`
- 숫자와 문장은 코드가 만든다. LLM에게 계산을 맡기지 않는다
- 유보는 `status: held`와 사유(코드·한국어 뜻)로 남는다. 0점이 아니다
- DuPont은 데이터 경로 환경변수(.env)가 없으면 `status: not_computed`로 남는다
- 임계 모델은 점수가 아니라 경보라서 `risk_score`가 없다
- 투자자 의견은 원문 그대로 `untrusted: true`로 넣는다. 주장 추출은 분석 단계에서 한다
- 경제명제(`propositions`)는 `--graph`일 때 채운다 (아래). 입력 4(`input4`)는 자리만 있다 (null)
- 형식은 `schemas/evidence_pack.schema.json`, 모델 설명서는 `model_cards.json` (코드 없이 고칠 수 있다)

## 경제명제 (`graph_props.py`, `--graph`)

투자자 의견의 인과 주장을 FDS 공통 인과그래프(Neo4j)에서 찾아 근거 `PR-n`으로 넣는다.

```bash
python backend/llm/build_pack.py 010170 2016-06-30 2019-06-28 --opinion "원자재 가격이 올라 영업이익률이 떨어질 것" --graph
python backend/llm/analyze.py 010170 2016-06-30 2019-06-28 --opinion "..." --graph
```

1. OpenAI가 의견에서 "원인 → 결과" 구절만 뽑는다 (예: 원자재 가격 증가 → 영업이익률 감소). 판정은 하지 않는다
2. 코드가 구절을 임베딩해 Neo4j 벡터 인덱스로 노드를 찾고, 그래프 경로·누적 부호·경로 위 카드의 깨지는 조건을 가져온다
3. 근거 문장은 코드가 만든다. 경로마다 `PR-n`(경로, 누적 부호, 주장한 방향과 같은지, 구절 짝짓기, 검토 상태)과 `PR-n+1`(상쇄할 수 있는 조건)
4. 경로나 확인 조건에 서서히 모델이 재는 변수가 있으면 `values.model_links`와 문장 끝에 어느 근거로 확인하는지 적는다 (예: 영업이익률 → DuPont PM [DP-1]). 연결표는 `graph_model_map.json`이며 코드 없이 고칠 수 있다
5. 분석 단계에서 PR은 **판정 근거가 아니다**. 판정은 지금처럼 모델 근거(FS/OH/DP/GT)로만 하고, PR은 주장마다 `proposition_ids`와 `graph_note`(그래프 경로와 확인할 조건)로 따로 단다. 코드가 PR을 `evidence_ids`에 쓰거나, graph_note에 모델 근거를 섞으면 잡아낸다

준비:
- Neo4j 5.23 이상에 FDS 그래프와 임베딩이 들어 있어야 한다. 띄우고 적재하는 법은 [backend/graph](../graph/README.md)
- `.env`: `NEO4J_PASSWORD`, `OPENAI_API_KEY` (선택: `NEO4J_URI` 기본 bolt://localhost:7687, `NEO4J_USER` 기본 neo4j, `NEO4J_DATABASE`, `OPENAI_EMBED_MODEL` 기본 text-embedding-3-small. 그래프를 임베딩한 모델과 같아야 한다)
- `pip install neo4j`
- 연결·설정이 없으면 `propositions.status: not_computed`와 사유가 남고 나머지는 그대로 돈다

시험: `pytest tests/llm/test_graph_props.py` (가짜 객체). 실제 Neo4j·OpenAI로는 `GRAPH_LIVE=1`을 붙인다.

## 한계

- 모델 데이터가 2015~2019년이라 그 밖의 날짜는 `warnings`가 붙고 대부분 유보가 된다
- 결과는 임시 규칙(`provisional-0`) 기준이고 통합 판정은 없다(D10)
- 경제명제: 그래프 카드 641개가 모두 검토 전 초안(draft)이다. 구절과 노드를 임베딩 유사도로 짝짓기 때문에 엉뚱한 변수에 붙을 수 있다 (예: '광통신 수요' → 통화가치 절하). 근거 문장에 짝짓기와 유사도 경고를 남긴다
- 경제명제: 그래프 변수와 모델 지표의 연결표(`graph_model_map.json`)는 확실한 8개만 넣었다. 총자산회전율(DuPont ATO)은 그래프에 맞는 변수가 없다
