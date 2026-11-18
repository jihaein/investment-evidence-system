# investment-evidence-system

보유 종목의 가치 훼손 위험을 근거로 점검하는 시스템의 모델 쪽 저장소. 지금 구현된 것은 **서서히 모델**(F-Score, Ohlson, DuPont)이다. 급락 모델과 반박 모델은 아직 없다.


## 구조

| 위치 | 역할 |
|---|---|
| `models/seoseohi/fscore.py` | F-Score 계산과 매수일·평가일 위험점수(0~3) |
| `models/seoseohi/ohlson/ohlson.py` | Ohlson RI·ω 비교와 위험점수(0~2) |
| `models/seoseohi/dupont.py`, `pipeline.py` | DuPont PM·ATO 비교와 위험점수(0~3) |
| `models/seoseohi/run_models.py` | 세 모델을 같은 표준 표로 돌리는 연결 파일 (`run_fscore`, `run_ohlson`, `run_dupont`, `run_all`)과 임계 모델 경보 `run_gate` |
| `models/seoseohi/threshold.py` | 임계 모델: 매수일 이후 주가가 꾸준히 나빠지는지 보는 경보기(켜짐/꺼짐, 점수 아님) |
| `models/seoseohi/link_fundamentals.py` | 가격 경보 뒤에 F-Score·Ohlson 악화가 확인되는지 보는 평가 |
| `models/seoseohi/export_contract.py` | 표준 표를 대시보드가 읽는 계약 JSON으로 내보냄 |
| `contracts/risk_result.schema.json` | 계약 JSON 스키마 |
| `tests/seoseohi/test_run_models.py` | 기대값, 유보, 계약 스키마 점검 |
| `data/` | 입력 CSV |

`models/seoseohi/output/`의 `ohlson_daily.csv`, `ohlson_eval.csv`는 옛 산출물일 수 있다. 현재 코드의 출력 위치는 `models/seoseohi/ohlson/output/`다.

## 준비

Python 3.12, `pandas`, `numpy`. DuPont은 `python-dotenv`도 필요하다. 테스트는 `pytest`, 계약 검증은 `jsonschema`.

```bash
pip install pandas numpy python-dotenv pytest jsonschema
```

입력 자료:
- F-Score, Ohlson은 저장소 `data/fscore`, `data/ohlson` 아래 CSV를 읽는다.
- DuPont은 환경변수 두 개로 경로를 받는다(`.env`에 써도 된다). 코드에 경로를 박지 않는다.
  - `SEOSEOHI_TTM_DIR`: `ttm_final.csv`가 있는 폴더
  - `SEOSEOHI_FINAL_DATA_DIR`: `12_기업개황_KSIC.csv`, `01_종목목록.csv`, `03_재무_분기_2015-2019.csv`가 있는 폴더

## 쓰는 법

```python
import sys; sys.path.insert(0, "models/seoseohi")
from run_models import run_all
pairs = [("010170", "2016-06-30", "2019-06-28")]      # (종목, 매수일, 평가일)
run_all(pairs)                                          # F-Score, Ohlson
run_all(pairs, include_dupont=True)                     # DuPont 환경변수 필요

from run_models import run_gate
run_gate(pairs)                                         # 임계 모델 경보. 점수가 아니라 켜짐/꺼짐이라 run_all에는 포함되지 않는다
```

점검과 계약 생성:

```bash
pytest tests/seoseohi/test_run_models.py
python models/seoseohi/export_contract.py               # contracts/risk_result.json 생성
python models/seoseohi/export_contract.py --buy 2016-06-30 --eval 2019-12-30 --stocks 053660
```

`export_contract.py`는 DuPont 환경변수가 없으면 DuPont을 계산하지 않고 `data_path_not_configured`로 남긴다. 생성된 `contracts/risk_result.json`은 Git에 올리지 않는다.

대시보드는 이 파일을 읽기만 한다. 대시보드 쪽에서는 환경변수 `SEOSEOHI_RESULT_JSON` 또는 `start.ps1 -ModelResultJson`에 파일 경로를 준다.

## 알려진 한계

- 임계 모델(`run_gate`)은 점수가 아니라 경보다. 합산하지 않으며, 본 데이터(19종목 × 5년)에서 경보는 이후의 큰 하락이나 F-Score·Ohlson 악화를 예고하지 않았다(`link_fundamentals.py`). "가격이 이미 약해졌다"는 상태 알림으로만 읽는다. 경보가 며칠만 켜졌다 꺼지므로 `alarm_on`(평가일 상태)보다 `alarm_in_hold`(보유 중 발생 여부)로 읽는 편이 안전하다.
- 점수 눈금이 모델마다 다르다(0~3, 0~2, 0~3). 이 계약은 합산하지 않으며 통합 판정은 D10 전까지 보류다.
- DuPont의 ATO는 평균 총자산 기준이고, 업종 중위수에 대상 기업이 포함된다(D08, D09).
- 한쪽 지표만으로 낸 DuPont 점수는 `partial=True`로 표시된다.
- Drive `서서히모델_v0`와 입력·정의·점수표가 달라 값이 일치하지 않는다. 자세한 비교는 대시보드 저장소 `docs/INTEGRATION_DESIGN.md` 7절.
- 팀에 확인할 질문은 대시보드 저장소 `docs/TEAM_QUESTIONS.md`.
