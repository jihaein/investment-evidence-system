"""Import an existing historical data-team handoff; use live_refresh for scheduled updates."""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ALL_STEPS = [
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
        if subprocess.run([sys.executable, script], cwd=ROOT).returncode != 0:
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
