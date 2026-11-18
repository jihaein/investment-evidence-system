# 서서히 데이터 파이프라인 (data_pipeline/)

리테일 투자자용 재무 훼손 탐지 모델 '서서히'의 데이터 계층.
원천 데이터를 PostgreSQL(Supabase)로 적재하고, 모델 신호를 사전계산해 앱이 조회할 수 있게 한다.

---

## 1. 아키텍처 한눈에

```
  원천 데이터 (CSV, 스냅샷)            Supabase                      앱 / API
  ┌───────────────────┐        ┌────────────────────────┐
  │ 가격 CSV          │──적재─▶│ PostgreSQL              │
  │ 기업개황(KSIC)    │        │  · stocks  (종목 마스터)│◀─ FastAPI (backend/api.py)
  │ 기준일별 스냅샷   │─(모델)▶│  · prices  (일별 시세)  │      /signal 조회
  │ (DuPont/Ohlson/F) │        │  · signals (서서히 판정)│
  └───────────────────┘        │ Storage (원천 원본 아카이브)
                               └────────────────────────┘
```

**핵심 설계 원칙**: *쿼리하는 데이터*는 테이블(Postgres), *모델 입력용 원본 파일*은 Storage.
원천 스냅샷은 앱이 직접 조회하지 않고, 모델이 읽어 `signals`(출력)만 테이블에 넣는다.

---

## 2. 테이블 스키마

| 테이블 | 내용 | 주요 컬럼 | 행 수 |
|---|---|---|---|
| `stocks` | 종목 마스터 | stock_code(PK), stock_name, market, ksic_code, corp_code | 120 |
| `prices` | 일별 시세 | (stock_code, bas_dd) PK, mktcap, close_prc, quality_status | 182,823 |
| `signals` | 서서히 판정(사전계산) | (stock_code, baseline_date, eval_date) PK, dupont/ohlson/fscore, sum_total, verdict_count, verdict_sum | 45,360 |

`signals`는 **모든 (종목 × 매수기준선 스냅샷 × 평가 스냅샷)** 조합을 담는다.
→ 유저가 아무 날짜에 사도, 가장 가까운 스냅샷으로 '스냅'해서 즉시 조회 가능 (출시용 설계).

---

## 3. 스크립트

| 스크립트 | 하는 일 | 실행 |
|---|---|---|
| `load_prices.py` | 가격 CSV → `prices` + `stocks`(기본정보) | `.venv/bin/python data_pipeline/load_prices.py` |
| `load_stocks.py` | 기업개황(KSIC) CSV로 `stocks` 보강(upsert) | `.venv/bin/python data_pipeline/load_stocks.py` |
| `compute_signals.py` | 3개 모델 돌려 `signals` 사전계산·적재 | `.venv/bin/python data_pipeline/compute_signals.py` |
| `archive_snapshots.py` | 원천 스냅샷 트리를 zip 1개로 묶음(→ Storage 업로드용) | `.venv/bin/python data_pipeline/archive_snapshots.py` |
| `supabase_setup.sql` | RLS 읽기 정책 + 인덱스 (Supabase SQL Editor 에서 실행) | 대시보드 SQL Editor |

**최초 적재 순서**: `load_prices` → `load_stocks` → `compute_signals`.
(가격이 stocks 를 만들고, KSIC 가 보강하고, 신호는 stocks 를 참조하므로.)

---

## 4. 중요한 설계 결정

- **PIT(기준일별 스냅샷)** — 미래정보 참조(look-ahead) 방지. 각 기준일은 "그 시점에 공시된 재무"만 본다.
- **생존편향 통제** — 120종목 중 21개가 기간 중 상장폐지/거래정지(데이터에 실재). 엔진이 회수율로 청산 반영.
- **테이블 vs Storage** — 쿼리용 정형데이터만 테이블, 원천 파일 트리(463개, 207MB)는 Storage 아카이브.
- **upsert vs truncate** — `load_stocks`는 upsert(있으면 갱신). `load_prices`·`compute_signals`는 아직 truncate+재적재(아래 '남은 일' 참고).

---

## 5. 남은 일 (DB 셋업 완성용)

1. **Storage 아카이브** — `archive_snapshots.py` 로 zip 만들고 Supabase Storage 버킷에 업로드 → 코드의 Google Drive 경로 의존 제거. (데이터 207MB → 무료 Storage 1GB 안에 들어감)
2. **증분 업데이트(upsert)** — `load_prices`·`compute_signals`를 truncate 대신 upsert 로. (MORNING_BRIEF.md 에 적용 코드 있음. ⚠️ load_prices 가 stocks 의 KSIC 보강을 덮어쓰지 않도록 주의.)
3. **RLS·인덱스** — `supabase_setup.sql` 실행 (앱이 Supabase 직접 조회 시 필요).

---

## 6. 환경

- `.env` 에 `SUPABASE_DB_URL` (Session pooler, `postgresql+psycopg://...`), 드라이버는 `psycopg[binary]`.
- venv: `~/Developer/investment-evidence-system/.venv` (Python 3.12).
- `.env` 는 절대 커밋 금지 (gitignore 되어 있음).
