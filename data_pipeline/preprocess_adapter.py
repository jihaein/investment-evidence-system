"""
preprocess_adapter.py — 데이터팀 preprocess() 연결 지점(어댑터).
raw 수집 결과(_raw_pull) → 계약서 6개 CSV(RAWDATA_DIR)로 변환하는 단계.

[데이터팀 모듈 받으면 할 일 — 여기 한 곳만 수정]
  1) 데이터팀 모듈을 설치하거나 PYTHONPATH 에 둔다.
  2) USE_REAL = True 로 바꾸고, 아래 import 줄을 실제 모듈/함수 이름으로 맞춘다.
  3) run_pipeline.py 다시 실행 → validate_contract 가 규격을 자동으로 확인한다.

지금(스텁): 이미 있는 계약 CSV를 그대로 쓴다(통과). '연결 배선'을 미리 검증하는 용도.
실행(단독): .venv/bin/python data_pipeline/preprocess_adapter.py
"""
import os
import glob
from dotenv import load_dotenv

ROOT = "/Users/jihaein/Developer/investment-evidence-system"
load_dotenv(f"{ROOT}/.env")
RAWDATA_DIR  = os.environ["RAWDATA_DIR"]
RAW_PULL_DIR = os.path.join(RAWDATA_DIR, "_raw_pull")   # fetch_*.py 가 원본을 저장하는 곳

USE_REAL = False   # ← 데이터팀 모듈을 받으면 True 로


def preprocess(raw_dir: str = RAW_PULL_DIR, out_dir: str = RAWDATA_DIR) -> None:
    """raw_dir(수집 원본)  →  out_dir(계약서 6개 CSV)."""
    if USE_REAL:
        # ↓ 데이터팀이 준 모듈/함수 이름으로 바꾼다 (예시)
        from datateam_preprocess import preprocess as _real
        _real(raw_dir, out_dir)
        print(f"  preprocess 완료(데이터팀 모듈) → {out_dir}")
    else:
        n = len(glob.glob(os.path.join(out_dir, "*.csv")))
        print(f"  [stub] preprocess 건너뜀 — 기존 계약 CSV {n}개 사용 (연결 배선만 검증)")


if __name__ == "__main__":
    preprocess()
