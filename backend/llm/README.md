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
- 경제명제(`propositions`)와 입력 4(`input4`)는 자리만 있다 (null)
- 형식은 `schemas/evidence_pack.schema.json`, 모델 설명서는 `model_cards.json` (코드 없이 고칠 수 있다)

## 한계

- 모델 데이터가 2015~2019년이라 그 밖의 날짜는 `warnings`가 붙고 대부분 유보가 된다
- 결과는 임시 규칙(`provisional-0`) 기준이고 통합 판정은 없다(D10)
