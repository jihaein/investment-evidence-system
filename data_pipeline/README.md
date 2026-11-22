# data_pipeline

운영 설치·스케줄·복구는 [운영 안내](../docs/operations.md).

```
data_pipeline/
├── scheduled_refresh.py   매일 갱신 진입점: KRX 시세 + DART 공시 (ops/live/run_daily.sh가 실행)
├── live_refresh.py        세션별 시세 발행, 검토된 재무 스냅샷 발행, 롤백 (수동 명령도 여기)
├── dart_conversion.py     DART 접수번호 → 모델 입력 PIT 스냅샷
├── compute_signals.py     서서히 신호 계산
├── publication.py         DB 발행·롤백 (refresh_runs)
├── market_data.py         거래일 달력, 시세 파일 순서
├── load_prices.py         시세 CSV 읽기 (매일 갱신이 사용) + 과거 시세 일괄 적재
├── fetch/                 매일 갱신이 쓰는 수집기: fetch_krx.py, fetch_disclosures.py
├── migrations/            001_serving.sql (DB 스키마), supabase_read_policies.sql (선택: 읽기 정책)
├── seed/                  최초 1회 적재·재구축용 (매일 갱신에는 쓰이지 않음)
│   ├── run_pipeline.py        데이터팀 납품본 적재: validate → prices → stocks → signals → check
│   ├── validate_contract.py   6개 CSV 계약 검사
│   ├── load_stocks.py         종목 마스터 적재
│   ├── check_data.py          적재 후 점검
│   ├── build_metadata.py      preprocess_metadata.json 생성 (dart_conversion이 읽음)
│   ├── preprocess_adapter.py  6개 CSV 릴리스 생성 (검토용)
│   └── fetch_company.py, fetch_financials.py, fetch_capital.py   과거 DART 원본 일괄 수집
└── tools/
    └── parity_dupont.py   DuPont 스냅샷 방식 vs 플랫 CSV 방식 점수 대조
```

자주 쓰는 명령 (저장소 맨 위에서):

```bash
python -m data_pipeline.scheduled_refresh --once                 # 지금 한 번 갱신
python -m data_pipeline.live_refresh market --session 2026-11-20 # 특정 세션만 다시 발행
python -m data_pipeline.live_refresh rollback <RELEASE_ID>       # 최근 발행 되돌리기
python -m data_pipeline.seed.run_pipeline                        # 새 DB에 과거 데이터 적재
```
