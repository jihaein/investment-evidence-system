# 서서히 데이터 파이프라인 (data_pipeline/)

원천 데이터를 PostgreSQL(Supabase)로 적재하고, 모델 신호를 사전계산해 앱이 조회할 수 있게 한다.

---

## 1. 아키텍처

```
데이터팀 전처리 데이터          data_pipeline            Supabase                앱/API
(RAWDATA_DIR)      →   load/compute 스크립트  →  PostgreSQL + Storage  →  backend/api.py
```

- **쿼리용 정형데이터**(가격·종목·신호) → Postgres 테이블
- **모델 입력 원본 파일** → Supabase Storage (+ 로컬 `RAWDATA_DIR`)
- 데이터팀이 전처리(DART/KRX pull → TTM·PIT 스냅샷·FLUC_RT) 후 전달 → 이 파이프라인이 적재.

---

## 2. 스크립트

| 스크립트 | 역할 | 방식 |
|---|---|---|
| `run_pipeline.py` | **갱신 오케스트레이터** (아래 스텝을 순서대로 실행) | — |
| `load_prices.py` | 가격 CSV → `prices` · `stocks` | upsert |
| `load_stocks.py` | 기업개황(KSIC) → `stocks` 보강 | upsert |
| `compute_signals.py` | 3개 모델 실행 → `signals` 사전계산 | upsert |
| `archive_snapshots.py` | 원본 스냅샷 → zip (Storage 업로드용) | — |
| `supabase_setup.sql` | RLS 읽기 정책 + 인덱스 | — |

---

## 3. 테이블

| 테이블 | 내용 | 행 수 |
|---|---|---|
| `stocks` | 종목 마스터 (이름·시장·KSIC·DART `corp_code`) | 120 |
| `prices` | 일별 시세 | 182,823 |
| `signals` | 서서히 판정 (종목 × 매수기준선 × 평가일 전조합) | 45,360 |

---

## 4. 갱신 워크플로우 (새 데이터를 받았을 때)

데이터팀이 전처리한 데이터를 전달하면:

1. 받은 데이터를 **`RAWDATA_DIR`** 에 넣는다(기존 것 교체). 위치는 `.env` 의 `RAWDATA_DIR`.
2. 한 번에 갱신:
   ```bash
   # 전체 (새 재무가 들어왔을 때): 가격 → 종목 → 신호
   .venv/bin/python data_pipeline/run_pipeline.py

   # 가격만 (매일): 빠름(~1분)
   .venv/bin/python data_pipeline/run_pipeline.py --prices
   ```
   upsert 라 재실행해도 안전(중복/삭제 없음). 한 단계라도 실패하면 자동 중단.

### 권장 주기
| 데이터 | 주기 | 이유 |
|---|---|---|
| 가격(prices) | **매일** (`--prices`) | 시가총액·PBR·포트폴리오 현재가 (Ohlson 일부가 당일 시총 사용) |
| 신호(signals) | **새 재무 공시 때만** (전체 실행, ~15분) | 판정은 분기 재무가 바뀔 때만 움직임 — 매일 돌릴 필요 없음 |

### 자동 스케줄 (예시)
상시 켜진 **서버**에서 cron 으로. (노트북은 꺼지면 안 돌아가므로 비권장)
```cron
# 평일 저녁 7시(장마감 후) 가격 갱신
0 19 * * 1-5  cd ~/Developer/investment-evidence-system && .venv/bin/python data_pipeline/run_pipeline.py --prices
```
신호 재계산은 데이터팀의 '새 재무 전달' 이벤트에 맞춰 수동/트리거 실행.

---

## 5. 설계 결정

- **PIT 스냅샷** — 미래참조(look-ahead) 방지.  **생존편향 통제** — 상장폐지 종목 포함(엔진이 청산 반영).
- **테이블 vs Storage** — 쿼리용은 테이블, 원본 파일 트리는 Storage.
- **upsert** — 전 스크립트가 '있으면 갱신/없으면 추가'. truncate 안 함(실패해도 기존 보존, KSIC 보강 보호).

---

## 6. 환경
- venv Python 3.12, `psycopg[binary]` + `sqlalchemy`
- `.env`: `SUPABASE_DB_URL`(`postgresql+psycopg://`), `RAWDATA_DIR`(로컬 원본 폴더)
- `.env`·원본데이터·캐시는 커밋 안 함(.gitignore).
