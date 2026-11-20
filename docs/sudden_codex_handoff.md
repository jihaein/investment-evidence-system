# 급락 모델 인계 문서 (Codex용, 2026-11-20)

## Codex 후속 수집 복구 현황 (2026-11-20)

**현재 사용자 지시(후속 승인): 남은 결측을 기록하고 현재 캐시를 동결해 최종 판정·백테스트까지 진행한다.**
외부 실패 677건과 네이버 실패 1건을 명시하는 조건으로, 기존 수집 실패 수 기준에 대한 예외를 사용자가 승인했다.
판정 설정은 변경하지 않는다. 실행 상태는 `cache/final_trial_20261120/`의 단계별 JSON과 로그를 먼저 확인하고,
완료 단계를 다시 실행하지 않는다. `run_final_trial.py`는 완료 영수증이 있는 단계를 건너뛴다.

- `git pull --ff-only` 완료. 반복 수집기·래퍼·상태 감시 종료. 커밋/push 없음.
- 원본 `cache/naver_120/`은 보존했다. 외부 실패는 네이버 파싱 실패 1건과 별개로 1,961건이었다.
- 저장 HTML 복구: `cache/naver_120_offline_recovered_20261120_111157/`, 839건 전체 조사 중 754건 복구.
- HTML 이동 복구: **최신 파생 캐시 `cache/naver_120_redirect_recovered_20261120/`**, 537건 중 530건 복구.
  같은 기사번호·검색 제목·robots를 검증하고 호스트별 6초 간격으로 제한 재수집했다.
  더케이피엠→포인트데일리 이동은 원 서버 응답과 기사 동일성을 확인한 관계만 허용했다.
- 사건 창 기사 연결: 원본 12,060 → HTML 복구 후 12,457 → 이동 복구 후 12,730.
- 기사 0건 사건: 439 → 428 → 413 / 1,214건. 외부 실패는 677건, 네이버 파싱 실패는 1건 남았다.
- 최신 캐시의 `README_recovery.txt`, `recovery_report.json`, `event_article_count_changes.csv`에 결과와 한계를 기록했다.
  원본과 이전 파생 캐시도 원문 참조 때문에 함께 보존해야 한다.
- 운영 파서·판정 설정·일일 실행은 변경하지 않았다. 복구 스크립트와 원문/기록은 `cache/` 아래에만 있다.
- 시세·지수 복원 완료: `cache/krx_final/` (주가 4,952,091행, 지수 3,922행, 필요한 시장·거래일 지수 누락 0).
- 입력·코드 해시는 `cache/final_trial_20261120/freeze.json`에 동결했다. 기존 급락 테스트 307개 통과.
- N·P/공시 실행을 시작했다. N·P의 미완료 순차 호출은 기존 응답을 보존한 채 중단했고,
  `cache/prewarm_final_facts.py --resume-final`이 같은 기사·모델·지시문 캐시 14,989개를 16개 작업으로 채운 뒤 기존 코드를 재개한다.
  실행 방식만 바뀌었으며 판정 규칙·모델·입력은 동일하다. `facts_prewarm.json`과 `dart.json`에서 진행 상태를 확인한다.
- **2026-11-20 21시 완료**: N·P(금액 문자열 결측 처리 수정, freeze.json amendments 기록) → ACH 1회(연결 오류 1회 후 이어 돌림) → 계약 전체/10건 제외판 → 스트레스·몬테카를로. 판정 가치훼손 93 / 비가치 434 / 판단보류 687. 결과는 `models/sudden/README.md` "파이널 시험" 절, 숫자는 `cache/final_trial_20261120/final_report.json`. 급락 테스트 308개 통과. 커밋 없음.

---

이 문서를 처음부터 끝까지 읽고 **0절의 규칙을 지키면서** 3절의 작업을 순서대로 하라.
저장소: `C:\code\investment-evidence-system` (Windows, PowerShell. Git Bash도 있음). 브랜치: `sudden/crawl-all-stocks`.
Python은 항상 `.\.venv\Scripts\python.exe`를 쓴다. 모든 명령은 저장소 루트에서 실행한다고 가정한다.
사용자는 한국어로 대화한다. 보고도 한국어로 한다.

---

## 0. 반드시 지킬 규칙

1. **main에 merge 금지.** 이 브랜치를 main에 합치지 마라. (origin/main을 이 브랜치로 가져오는 것은 괜찮다.) push도 하지 마라.
2. **커밋은 사용자가 요청할 때만.** 커밋할 때도 다음 파일은 절대 커밋하지 마라:
   - 루트 `README.md` (사용자 개인 수정)
   - `models/sudden/crawl_summary.html`
   - `models/sudden/output_2023/`·`output_2026/`의 `drop_candidates.csv`, `drop_events_all.csv`, `drop_sessions_all.csv`
3. **기사 본문·제목·인용은 `models/sudden/cache/`(gitignore) 안에만 둔다.** 기사 인용이 든 검수 HTML 등을 공개 위치에 올리지 마라.
4. **`.env`를 열어 값을 출력하지 마라.** 키 이름만 확인해도 된다. `.env` 수정은 사용자가 직접 한다. 자격 증명을 입력하지 마라.
   현재 `.env` 키: `OPENAI_API_KEY KRX_ID KRX_PW OPENDART_API_KEY NAVER_API_HUB_CLIENT_ID NAVER_API_HUB_CLIENT_SECRET SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY BACKTEST_DATA_DIR BACKTEST_DATA_DIR_2026`
5. **2020~2026(120종목)은 "파이널 시험"이다.** 아래 1.3절의 고정 설정으로 **딱 한 번** 판정·백테스트한다.
   결과를 본 뒤 설정(지시문, 옵션, 회사명 규칙, 임계값 등)을 바꿔 다시 돌리지 마라. 코드 버그로 실행이 실패하면 고쳐서 다시 돌려도 되지만,
   **판정 규칙에 영향을 주는 변경은 금지**하고, 고친 내용은 README에 기록한다.
6. 팀원 코드(`models/seoseohi/`, `backtest/`)의 기본 동작은 바꾸지 않는다(옵션 추가만 허용, 이미 그렇게 해 둠).
7. 매일 자동 실행(Windows 작업 스케줄러, 평일 18:30 `models\sudden\run_live_daily.cmd`)은 건드리지 않는다.

---

## 1. 지금까지 한 일 (요약)

### 1.1 모델 구조
- 급락 감지(`events.py`) → 뉴스 수집(`crawl_plan.py`, `naver_crawl.py`) → N·P 점수(`np_batch.py`) → 공시(`dart.py`) → ACH 판정(`ach.py`) → 결과 형식(`export_sudden_contract.py`) → 백테스트(`stress.py`, `joint_backtest.py`).
- ACH: LLM은 기사 단위 분류만 하고, 판정(가치훼손 / 비가치 / 판단보류)은 코드가 관계표 `_REL`로 계산한다.
- 자세한 이력은 `models/sudden/README.md`(특히 "팀 120종목 백테스트", "사람 검수와 분류 지시문 4판", "파이널 설정" 절)와 `docs/sudden_model.md`.

### 1.2 개발·튜닝 결과 (확정)
- 개발 표본 19종목 2016~2019: 급락 전체 +12.9%, MDD 0.184 (보유 +7.6% / 0.446). 몬테카를로·워크포워드·스트레스에서는 무작위 매도와 크게 구분되지 않음.
- 사람 검수 2회: 지시문 4판은 채택 기준 미달 → 3판 유지. 회사명 엄격 매칭(`news_review.name_in`, `STRICT_NAMES=True`) 채택.
- 급락+서서히 결합 시험: 기준 미달 → 파이널 설정 변경 없음.
- 임계 모델 120종목 2020~2026 스트레스: 무작위 매도 대비 승률 0.19~0.27 (단독 매도 신호로 부적합). 이미 README에 기록됨.

### 1.3 파이널 고정 설정 (2026-11-17 동결, 변경 금지)
- 분류 지시문 `ach-units-3` (ach.py 기본값)
- `ach.py --model gpt-5-mini --market-only-holds --min-support-ratio 0.25`
- `--prompt`, `--neutral-zero`, `--positive-*`, `--gap` 옵션은 **쓰지 않는다**
- 회사명 엄격 매칭 `news_review.STRICT_NAMES = True` (현재 코드 값)

### 1.4 파이널 표본 준비 (완료)
- 120종목: `models/sudden/output_120/team_codes.txt`
- 급락 사건: `output_120/drop_events.csv` — 1,214건, 98종목, 2020-01-01~2026-11-16, 시총 1,000억 이상
- 수집 계획: `output_120/crawl_plan.csv` (검색 24,115번)
- 튜닝·검증 표본과 겹치는 사건 10건: `output_120/overlap_with_tuning.csv` (2023 표본 4건, 2026 표본 6건). **최종 결과는 전체와 이 10건 제외, 두 가지로 보고한다.**

### 1.5 뉴스 수집 (2026-11-20 완료)
- 캐시: `models/sudden/cache/naver_120/` (`crawl.sqlite3`, `searches.jsonl`, `articles.jsonl`, `event_links_<종목>.jsonl` 98개, `summary_<종목>.json` 98개, 본문 `*.txt`)
- 수집기: `models/sudden/cache/run_120_2026_collect.py` (naver_crawl.py를 실패 시 30분 쉬고 재시도, delay 6초). 로그: `cache/run_120_2026_collect.log`
- 상태 확인: `.\.venv\Scripts\python.exe models\sudden\cache\crawl_status.py` → 검색 24,115 / 24,115 (100%)
- **주의: 수집기가 아직 끝나지 않고 30분마다 재시도 중일 수 있다.** 원인은 기사 1건
  (`https://n.news.naver.com/mnews/article/056/0011227368`)이 매번 `parse_failed`가 되어 naver_crawl.py가 0이 아닌 코드로 끝나기 때문이다
  (검색은 모두 끝났고 `pending_articles`는 0). 3.0절에서 이 프로세스를 멈춘다.
- 11/18 12:38경 네이버 403 차단이 있었다(이후 delay 3→6초, 대기 30분으로 바꿔 재개). 이 시간대에 저장된 검색 결과가 정상인지 3.1절에서 확인한다.

### 1.6 기타 진행 중인 것 (이번 작업과 별개, 건드리지 말 것)
- 매일 실행(`live.py`) + NAVER API HUB 병행 검증(`live_api_shadow.py`, 결과 `cache/live/<날짜>/api_compare.json`). 12/2쯤 평가 예정. 설계: `docs/sudden_live_api.md`.

### 1.7 커밋되지 않은 변경 (커밋은 사용자 요청 시만)
`git status`로 보이는 변경 대부분이 이번 작업의 코드다(stress.py, seoseohi_signal.py, news_api.py, live_api_shadow.py, ach.py 등).
테스트: `.\.venv\Scripts\python.exe -m pytest tests/sudden -q` → 307개 통과 상태. (`tests/seoseohi`, `tests/llm`의 실패 4개는 main에서 온 기존 실패라 무시.)

---

## 2. 남은 일 (순서)

| 순서 | 일 | 결과물 |
|---|---|---|
| 3.0 | 수집기 프로세스 정리 | 프로세스 없음 |
| 3.1 | **수집 품질 확인** (사용자가 특히 요청) | `cache/naver_120/crawl_check.json` + 사용자 보고 |
| 3.2 | 시세 파일 다시 만들기 | `cache/krx_final/prices_20181201_20261116.csv`, `index_...csv` |
| 3.3 | N·P → 공시 → ACH 판정 (한 번) | `cache/naver_120/ach/` |
| 3.4 | 결과 형식 | `contracts/sudden_result_team120_2020_2026.json` (+ 10건 제외판) |
| 3.5 | 백테스트: 스트레스 + 몬테카를로 (전체 / 10건 제외) | `cache/stress_120_final/`, `cache/stress_120_final_excl/` |
| 3.6 | 기록: README에 파이널 결과 절 추가 | `models/sudden/README.md` |

---

## 3. 작업 방법

### 3.0 수집기 정리
```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select-Object ProcessId, CommandLine | Format-List
```
`run_120_2026_collect.py`, `keep_awake_run.py`(수집기를 감싼 것), `crawl_status.py --watch`가 보이면 **사용자에게 확인을 받고** `Stop-Process -Id <PID>`로 멈춘다.
(이전 세션에서 자동 승인 정책이 프로세스 종료를 막았다. 막히면 사용자에게 직접 멈춰 달라고 하거나 PID를 알려 준다.)
`live.py`·`live_api_shadow.py`·`run_live_daily.cmd` 관련 프로세스는 멈추지 마라.

### 3.1 수집 품질 확인 (판정 전에 반드시)
판정 결과는 보지 않고 **수집 데이터만** 본다. 아래를 계산해 `models/sudden/cache/naver_120/crawl_check.json`에 저장하고 사용자에게 표로 보고한다.
스크립트는 `models/sudden/cache/` 아래에 만든다(gitignore). 먼저 `naver_crawl.py`에서 `searches`/`articles` 저장 형식(`save()`, `connect()`, payload의 `status`, `results`, `priority`, 저장 시각 필드)을 읽고 맞춰라.

1. **검색 완료도**: `crawl.sqlite3`의 `searches` 상태별 개수(complete / empty / capped / 기타). `output_120/crawl_plan.csv`의 (종목, 날짜, 검색어) 전부가 searches에 있는지. 빠진 것 목록.
2. **차단 시간대 빈 결과**: `status == 'empty'`인 검색을 저장 시각별로 묶어 비율을 본다. 특히 **2026-11-18 12:00~15:00**에 empty 비율이 평소보다 높으면 차단 페이지가 빈 결과로 저장된 것이다.
   의심되는 검색 키 목록을 만들고, 수가 많으면 사용자에게 알리고 해당 검색만 다시 받는 방법을 제안한다(그 키를 searches에서 지우고 수집기를 다시 돌리면 그것만 다시 받는다. **실행 전 사용자 확인**).
   저장 시각 필드가 없으면 로그(`run_120_2026_collect.log`)의 `[검색]`/에러 줄 시각과 대조한다.
3. **기사 상태**: `article_statuses` 합계(complete / excluded_title_mismatch / parse_failed / request_failed / external_failed). 실패 기사 목록. 위 `056/0011227368` 1건 외에 더 있는지.
4. **사건별 근거 기사 수**: `event_links_*.jsonl`로 1,214건 각각의 사건 창(우선순위 1) 기사 수. 0건인 사건 수와 비율, 종목별 분포.
   `event_statuses`의 `time_unknown`, `modified_after_decision` 합계.
5. **비교 기준**: 같은 지표를 2023 표본(`cache/naver_2023`, `output_2023`)과 2026 표본(`cache/naver_2026`, `output_2026`)에서도 계산해 나란히 보여 준다. 이 비교는 수집량만 보고 판정 결과는 보지 않는다.
6. **연도별 기사 수**: 2020~2026 연도별 사건 창 기사 수(오래된 해에 기사가 크게 적은지).
7. 회사명이 짧거나 흔한 단어인 종목(예: 2~3글자)에서 `excluded_title_mismatch` 비율이 지나치게 높거나 낮은 종목 상위 10개.

**판단 기준**: 빠진 검색 0, 실패 기사 극소수(수 건), 차단 의심 empty가 없거나 재수집 완료, 사건 창 기사 0건 비율이 2023/2026 표본과 비슷하면 "수집 정상"으로 보고 3.2로 간다.
문제가 있으면 **여기서 멈추고** 사용자에게 보고한다.

### 3.2 시세 파일 다시 만들기
수집·판정 스크립트가 쓰던 `cache/krx/prices_20181201_20261116.csv`, `index_20181201_20261116.csv`는 매일 실행(`live.py`의 `prune_price_windows`)이 지워서 지금 없다.
하루치 캐시(`cache/krx/daily/`, 2018-12-03~2026-11-19)가 있어 네트워크 없이 빨리 다시 만든다.
```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe models\sudden\krx_prices.py --start 2018-12-01 --end 2026-11-16
New-Item -ItemType Directory -Force models\sudden\cache\krx_final | Out-Null
Copy-Item models\sudden\cache\krx\prices_20181201_20261116.csv, models\sudden\cache\krx\index_20181201_20261116.csv models\sudden\cache\krx_final\
```
(`krx_final/`로 복사하는 이유: 매일 18:30 실행이 `cache/krx/`의 다른 날짜 파일을 다시 지운다.)
지수 파일이 krx_prices.py에서 안 만들어지면 `events.py`의 `INDEX_CACHE` 생성 방법을 확인한다. 만든 뒤 `output_120/drop_events.csv`를 다시 만들 필요는 없다(이미 있음, 다시 만들지 말 것).

### 3.3 판정 (한 번만)
PowerShell, 저장소 루트에서. 각 단계가 끝날 때마다 출력 마지막 줄을 확인하고, 실패하면 원인을 고쳐 **그 단계부터** 다시 한다.
```powershell
$env:PYTHONIOENCODING='utf-8'
$env:SUDDEN_OUTPUT='output_120'
$env:SUDDEN_PRICES='models/sudden/cache/krx_final/prices_20181201_20261116.csv'
$env:SUDDEN_INDEX='models/sudden/cache/krx_final/index_20181201_20261116.csv'
.\.venv\Scripts\python.exe models\sudden\np_batch.py --cache models\sudden\cache\naver_120
.\.venv\Scripts\python.exe models\sudden\dart.py
.\.venv\Scripts\python.exe models\sudden\ach.py --cache models\sudden\cache\naver_120 --model gpt-5-mini --market-only-holds --min-support-ratio 0.25 --out models\sudden\cache\naver_120\ach
```
- 오래 걸린다(LLM 호출 수천 번). 백그라운드로 돌리고 로그를 `models\sudden\cache\run_120_final_judge.log`에 남겨라. 중간에 끊겨도 LLM 답은 캐시되어 다시 돌리면 이어진다.
- `dart.py`는 `OPENDART_API_KEY`를 쓰고 결과를 `cache/dart/`에 캐시한다. 인자가 없고 `SUDDEN_OUTPUT`의 사건을 읽는다(코드에서 확인).
- `models/sudden/cache/run_120_2026_judge.sh`는 쓰지 마라(수집 단계가 들어 있고 시세 경로가 옛것이다).
- 판정이 끝나면 **판정 분포만** 기록한다(가치훼손 / 비가치 / 판단보류 개수, 판단보류 사유별). 결과를 보고 설정을 바꾸지 마라.

### 3.4 결과 형식
```powershell
.\.venv\Scripts\python.exe models\sudden\export_sudden_contract.py --ach models\sudden\cache\naver_120\ach --events models\sudden\output_120 --name team120_2020_2026
```
→ `contracts/sudden_result_team120_2020_2026.json`.
10건 제외판: 이 JSON의 `cases`에서 `output_120/overlap_with_tuning.csv`의 `drop_id`(= `<종목코드>_<YYYYMMDD>`)에 해당하는 사건을 뺀 사본을
`contracts/sudden_result_team120_2020_2026_excl_overlap.json`으로 저장한다. case의 사건 식별 필드 이름은 JSON을 열어 확인한다(종목코드+날짜 조합일 수 있음). 빠진 개수가 정확히 10인지 확인.

### 3.5 백테스트
팀 120종목 2020~2026 데이터 폴더는 `.env`의 `BACKTEST_DATA_DIR_2026` 값이다(값을 출력하지 말고 코드/환경변수로 넘긴다. 예: python-dotenv로 읽어 `--data-dir`에 전달하는 작은 래퍼, 또는 사용자에게 경로를 묻기).
참고로 경로는 `G:\내 드라이브\skku_fds_main\데이터\서서히 모델 백테스트\개발자전달_본데이터_120종목_백테스트_2020-2026` 이다.

스트레스 테스트 (팀 run_stress와 같은 규칙: 무작위 진입일 × 무작위 20종목, 150런, seed 7, 보유 1~4년):
```powershell
$D = '<BACKTEST_DATA_DIR_2026 경로>'
$P = 'models/sudden/cache/krx_final/prices_20181201_20261116.csv'
.\.venv\Scripts\python.exe models\sudden\stress.py --data-dir $D --threshold-prices $P --contract contracts\sudden_result_team120_2020_2026.json --horizons 1 2 3 4 --runs 150 --out models\sudden\cache\stress_120_final
.\.venv\Scripts\python.exe models\sudden\stress.py --data-dir $D --threshold-prices $P --contract contracts\sudden_result_team120_2020_2026.json --montecarlo --out models\sudden\cache\stress_120_final
```
같은 두 명령을 `--contract contracts\sudden_result_team120_2020_2026_excl_overlap.json --out models\sudden\cache\stress_120_final_excl`로 한 번 더.
- `--seoseohi`는 쓰지 않는다(19종목 전용).
- 출력: `stress_summary_k*_h*_seed7.json`(lag0/lag1), `stress_detail_*.csv`, `montecarlo_k*_h*.json`.
- 보고 지표(팀 형식, lag 1 = 다음 거래일 매도가 기본): 누적수익률, 수익률 중앙값, MDD, Sortino, Sharpe(rf=0), 평균 현금대체(cash_effect), 평균 매도 수,
  무손절 대비 승률, 같은 수 무작위 매도 대비 승률. 전략: 무손절 / 단순손절 / 임계 / 급락 / 임계_그리고_급락.
- 임계 단독 결과는 이미 README "팀 120종목 백테스트" 절에 있다. 같은 값이 나오는지(재현) 확인하고, 다르면 원인(시세 파일 차이)을 적는다.

### 3.6 기록
`models/sudden/README.md`의 "파이널 설정 (2026-11-17 고정)" 문단 뒤에 `## 파이널 시험: 120종목 2020~2026 (날짜)` 절을 추가한다:
1. 수집 품질 확인 요약(3.1 표)
2. 판정 분포
3. 스트레스 표(보유 1~4년 × 전략, 위 지표) — 전체 / 10건 제외 두 판
4. 몬테카를로 표(2020 진입 2·4년, 워크포워드 2020~2024 진입)
5. 결론: 급락 판정 매도가 무작위 매도·무손절보다 나은지. 개발 표본(19종목) 결과와 같은 방향인지.
문장은 짧고 평이하게, 기존 README 문체(한국어, "~다" 체)를 따른다. 기사 제목·본문 인용은 넣지 않는다.

---

## 4. 참고: 주요 파일

| 파일 | 역할 |
|---|---|
| `models/sudden/naver_crawl.py` | 네이버 뉴스 검색·본문 수집 (`--no-search`: 기존 검색만 사용) |
| `models/sudden/np_batch.py` | N·P 점수 (`--out-name`) |
| `models/sudden/ach.py` | 3차 ACH 판정. `SUDDEN_LLM_CACHE_DIR`로 LLM 캐시 위치 변경 가능 |
| `models/sudden/news_review.py` | 회사명 엄격 매칭 `name_in`, `STRICT_NAMES` |
| `models/sudden/export_sudden_contract.py` | 판정 → `contracts/sudden_result_*.json` |
| `models/sudden/stress.py` | 팀 규칙 스트레스·몬테카를로 (무손절/단순손절/임계/급락/결합) |
| `models/sudden/joint_backtest.py` | 단일 진입 백테스트, 팀 엔진 연결 |
| `models/sudden/seoseohi_signal.py` | 팀 서서히 신호 래퍼 (19종목 전용) |
| `models/seoseohi/threshold.py` | 팀 임계 모델 (`use_prices`로 시세 원천 변경) |
| `backtest/engine.py` | 팀 엔진 `run_strategy` |
| `models/sudden/cache/crawl_status.py` | 수집 진행 상태 (`--watch`) |
| `models/sudden/output_120/` | 파이널 표본(사건, 수집 계획, 겹치는 사건 목록) |

## 5. 끝나면 사용자에게 보고할 것
1. 수집 품질 확인 결과(정상/문제, 숫자 표)
2. 판정 분포
3. 백테스트 핵심 표(전체 / 10건 제외)와 한두 줄 결론
4. 만든 파일 목록. 커밋은 하지 않았다는 것(요청 시 0절 2번 제외 목록을 지켜 커밋)
