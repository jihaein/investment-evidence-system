# investment-evidence-system

보유 종목의 가치 훼손 위험을 점검한다. 서서히 모델(F-Score, Ohlson, DuPont)이 매일 KRX 시세와 DART 공시로 다시 평가되고, 결과는 Supabase에 발행되어 API와 LLM 설명이 읽는다. 모든 데이터는 Supabase(DB + Storage)에 있고, 이 컴퓨터의 데이터 폴더는 작업용 사본이다.

## 구조

| 위치 | 역할 |
|---|---|
| `data_pipeline/` | 매일 갱신(`scheduled_refresh.py`)과 그 부품(수집 `fetch/`, DART 변환, 신호 계산, 발행·롤백), DB 스키마(`migrations/`), 최초 적재 `seed/`, 분석 도구 `tools/`. [README](data_pipeline/README.md) |
| `models/seoseohi/` | 서서히 모델: `fscore.py`, `ohlson/ohlson.py`, `dupont.py`, 통합 판정 `pipeline.py`, 임계(가격 경보) `threshold.py`, 연구용 연결 `run_models.py` |
| `models/sudden/` | 급락 모델 연구(사건 탐지, 네이버 뉴스 수집, N/P 지표). 운영 판정 없음. [README](models/sudden/README.md) |
| `backend/api.py` | 읽기 전용 API (`/signal`, `/portfolio`, `/ready` …) |
| `backend/llm/` | 투자자 의견을 발행된 신호로 검증하는 LLM 분석. [README](backend/llm/README.md) |
| `seoseohi_preprocess/` | 데이터팀 전처리 엔진. DART 변환이 재사용한다 |
| `backtest/` | 백테스트 엔진·지표·몬테카를로, 실험 노트북 |
| `ops/live/` | 이 Mac의 매일 실행(launchd), 상태 점검, DB 마이그레이션, 거래일 달력·종목 범위 |
| `ops/cloud/` | Supabase Storage 동기화(`sync.py`)와 복원 검증(`restore_test.py`) |
| `ops/staging/` | Render 스테이징 배포 설정 |
| `backend/graph/`, `data/fds_graph/` | FDS 공통 인과그래프 원본(JSON)과 Supabase 적재 스크립트. LLM 분석의 경제명제(반박 근거)에 쓴다. [README](backend/graph/README.md) |
| `contracts/` | 연구용 결과 계약 JSON 스키마 |
| `data/` | 저장소에 포함된 소형 연구 입력(`fscore`, `ohlson`, `llm`). `data/rawdata`는 Git 밖, 클라우드에서 복원 |
| `tests/` | `pytest` |

## 처음 설정 (새 컴퓨터)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env            # 값 채우기: Supabase DB/Storage, OpenDART, KRX
python ops/cloud/sync.py pull   # 모든 데이터 폴더를 클라우드에서 복원
python ops/cloud/sync.py status # 전부 IN SYNC 인지 확인
pytest
```

## 매일 운영

- **GitHub Actions**(`.github/workflows/daily-refresh.yml`)가 평일 08:30, 19:30, 22:30(KST)에 돈다: 클라우드에서 빠진 파일 채우기 → 거래일 달력 연장 → KRX·DART 갱신과 발행 → 클라우드 업로드 → 최신 거래일 발행 확인. 실패하면 GitHub가 이메일을 보낸다.
- 손으로 돌리기: Actions → daily-refresh → Run workflow ("Only test the KRX download"를 고르면 발행 없이 KRX 다운로드만 확인).
- 켜고 끄기: 저장소 변수 `CLOUD_SCHEDULE_ENABLED`(true면 예약 실행). 비밀값 6개는 Settings → Secrets and variables → Actions.
- **두 곳에서 동시에 돌리지 않는다.** Mac용 launchd 설정(`ops/live/com.jihaein.ies-refresh.plist`, `run_daily.sh`)은 예비로 남겨 두었고 꺼져 있다.
- 점검: `python ops/live/check_status.py`, `python ops/live/verify_signals.py`
- 보류된 공시: DART 스냅샷 폴더의 `pending_triage.json` (`needs_review`만 사람이 볼 것). DART가 원문 문서를 주지 않는 접수번호는 `_raw_pull/dart/document_missing.json`에 기록만 한다.
- 거래일 달력은 매 실행마다 1년 앞까지 자동으로 늘어난다(`ops/live/update_calendar.py`). KRX의 임시 휴장은 `ops/live/krx_closures.csv`에 한 줄 추가한다.
- 자세한 설치·복구·롤백: [운영 안내](docs/operations.md)

## 데이터 위치

| 데이터 | 위치 |
|---|---|
| 시세, 신호, 발행 기록, LLM 분기표 | Supabase DB |
| `data/rawdata`, DART 스냅샷, 백테스트·Google Drive 원자료, 로그 | Supabase Storage `ies-data` (`ops/cloud/datasets.json`) |
| 비밀값 | `.env` (Git 제외, 비밀번호 관리자에 따로 보관) |

`sync.py push`·`pull`은 아무것도 지우지 않는다. `pull`은 기본적으로 없는 파일만 채운다(`--overwrite`로 교체). 클라우드 정리는 `sync.py prune`(미리보기) → `prune --yes`.

## LLM 분석

```bash
python backend/llm/analyze.py 095270 2025-12-01 --opinion "영업이익률이 좋아질 것"   # DB에 발행된 신호 기준
python backend/llm/analyze.py 010170 2016-06-30 2019-06-28 --legacy --opinion "..."   # 2015-2019 연구 데이터
```

## 연구용 모델 실행 (2015-2019)

```python
import sys; sys.path.insert(0, "models/seoseohi")
from run_models import run_all, run_gate
pairs = [("010170", "2016-06-30", "2019-06-28")]      # (종목, 매수일, 평가일)
run_all(pairs)                                          # F-Score, Ohlson (data/fscore, data/ohlson)
run_all(pairs, include_dupont=True)                     # DuPont: SEOSEOHI_TTM_DIR, SEOSEOHI_FINAL_DATA_DIR 필요
run_gate(pairs)                                         # 가격 경보(켜짐/꺼짐), 점수 아님
```

`python models/seoseohi/export_contract.py`는 연구용 계약 JSON(`contracts/risk_result.json`, Git 제외)을 만든다.

## 알려진 한계

- 운영 판정은 합산 규칙 `seoseohi-sum-v1`(합계 4 이상이면 `Yes(훼손 우려)`)이다. 모델 담당자 승인과 DuPont 기준값 대조가 남아 있다.
- F-Score는 연간 보고서 기준이라, 매수일과 평가일이 같은 사업보고서를 쓰면 유보된다.
- 새 사업보고서의 총장기부채는 DART 전산값이며, 주석과 대조하지 않는다(F-Score 레버리지 항목).
- 임계 모델은 경보일 뿐 합산하지 않는다. `alarm_in_hold`(보유 중 발생 여부)로 읽는다.
- 급락 모델과 브로커 화면 OCR은 아직 운영 경로에 없다.
