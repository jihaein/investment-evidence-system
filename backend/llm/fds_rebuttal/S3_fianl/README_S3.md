# S3 회사 논증 다시 만들기 (분기마다)

반박 파이프라인은 알림 날짜(`alert_date`)가 들어가는 창의 `company.argument<연도>-<n>.json`을 쓴다.
창은 4/6/9/12월 1일에 바뀐다(`2026-4` = 2026-12-01 ~ 2027-03-31). 새 창이 시작되기 전에 아래를 한다.

1. 데이터팀 CSV 최신본을 받는다: Google Drive `skku_fds_main/데이터/FDS_12개항목_<기간>/07_기업x분기_12개항목_결합.csv`
   → `backend/llm/fds_rebuttal/데이터/07_기업x분기_12개항목_결합.csv` (git에는 안 올라감)
2. `s3_config.json`의 `last_update`를 새 창 시작일로 바꾼다(4/6/9/12월 1일 중 하나. 2026-12-01 다음은 2027-04-01).
   `years`의 끝 연도가 그 해를 포함하도록 늘린다(2027년이면 `[2024, 2027]`).
3. 생성 (표준 라이브러리만 씀, 1분 안쪽):
   ```bash
   cd backend/llm/fds_rebuttal/S3_fianl
   python run_s3.py inspect          # 구조·결측 확인
   python run_s3.py all              # runs/full-2024-2026-<n>/ 에 전체 창 + fact_registry.json
   ```
4. 새 폴더의 파일로 `runs/full-2024-2026/`을 통째로 바꾼다(창마다 fact_id가 새로 매겨지므로 fact_registry.json과 함께).
5. 클라우드에 올린다(서버는 이걸 읽음): `python ops/cloud/sync.py push --only rebuttal_s3 rebuttal_inputs`

2026-11-23 확인: 같은 CSV로 다시 만든 2025-2·2026-3 창은 기존 파일과 노드·엣지 상태·방향·값이 전부 같았고
(달라진 건 fact_id 해시뿐), 2026-4 창(컷오프 2026-11-30 23:59 KST)이 새로 생겼다. 11-24~11-30에 늦게 낸
3분기 보고서나 정정이 있으면 12-01에 3~5단계를 한 번 더 한다.
