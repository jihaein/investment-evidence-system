# 서서히 데이터 파이프라인

운영 설치와 스케줄, 검증, 복구는 [운영 안내](../docs/operations.md)를 참고한다.

- `run_pipeline.py`는 기존 데이터팀 납품본을 가져온다. `--prices`는 디스크에 있는 시세를 적재한다.
- `live_refresh.py market`은 거래일별 시세를 수집하고 서서히 모형을 평가한다. 시세와 판정을 같은 트랜잭션으로 발행한다.
- `fetch/fetch_disclosures.py --poll --download`는 최근 DART 접수번호와 보고서 원문을 보존한다.
- `preprocess_adapter.py --out <경로>`는 새 6개 CSV 릴리스와 검토 목록을 만든다. 모델 입력에는 별도로 검증한 PIT 스냅샷이 필요하다.
- `python -m data_pipeline.scheduled_refresh`는 KRX 시세와 DART 정기공시를 주기적으로 갱신한다. DART 접수번호를 검증한 뒤 PIT 스냅샷을 만들고, 발행이 완료된 신호를 API에 반영한다.
- `live_refresh.py financial-inbox <경로>`는 별도로 검토한 예외 스냅샷을 발행한다.
- `migrations/001_serving.sql`은 신규 PostgreSQL 설치에 필요한 테이블과 버전 기록을 만든다. `supabase_setup.sql`은 기존 읽기 정책을 유지할 경우 사용하는 선택 설정이다.
