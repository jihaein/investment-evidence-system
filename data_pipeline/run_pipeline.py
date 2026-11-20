"""
데이터 갱신 파이프라인. 단계를 순서대로 실행하고 하나라도 실패하면 중단한다.
실행:
  .venv/bin/python data_pipeline/run_pipeline.py           # 전체: 전처리 → 계약검증 → 적재 → 신호 → 점검
  .venv/bin/python data_pipeline/run_pipeline.py --prices  # 시세만 (매일, 빠름)

흐름(전체):
  preprocess_adapter  수집 원본 → 계약서 6개 CSV   (데이터팀 모듈 연결 지점. 지금은 스텁)
  validate_contract   그 CSV가 계약 규격에 맞는지 검사 → 틀리면 여기서 중단(나쁜 데이터 차단)
  load_prices/stocks  Supabase 적재(upsert)
  compute_signals     3개 모형 판정 사전계산
  check_data          적재 후 건전성 점검
※ 원본 수집(fetch_*.py)은 pull 단계라 이 오케스트레이터와 분리돼 있다(별도 실행/스케줄).
"""
import argparse
import subprocess
import sys
import time

ALL_STEPS = [
    ("전처리(preprocess)",  "data_pipeline/preprocess_adapter.py"),
    ("계약 검증(validate)", "data_pipeline/validate_contract.py"),
    ("가격 적재(prices)",   "data_pipeline/load_prices.py"),
    ("종목 보강(stocks)",   "data_pipeline/load_stocks.py"),
    ("신호 계산(signals)",  "data_pipeline/compute_signals.py"),
    ("건전성 점검(check)",  "data_pipeline/check_data.py"),
]
PRICES_ONLY = [
    ("가격 적재(prices)",  "data_pipeline/load_prices.py"),
    ("건전성 점검(check)",  "data_pipeline/check_data.py"),
]


def run(steps):
    t0 = time.time()
    for i, (name, script) in enumerate(steps, 1):
        print(f"\n[{i}/{len(steps)}] {name}  ({script})", flush=True)
        if subprocess.run([sys.executable, script]).returncode != 0:
            print(f"\n실패: {name}, 파이프라인 중단")
            sys.exit(1)
    print(f"\n파이프라인 완료: {len(steps)}단계, {time.time() - t0:.0f}s")


def main():
    ap = argparse.ArgumentParser(description="서서히 데이터 갱신 파이프라인")
    ap.add_argument("--prices", action="store_true",
                    help="가격만 갱신(매일용). 지정 안 하면 전체 실행.")
    args = ap.parse_args()
    run(PRICES_ONLY if args.prices else ALL_STEPS)


if __name__ == "__main__":
    main()
