"""
run_pipeline.py — 데이터 갱신 파이프라인 오케스트레이터.
데이터팀이 전처리한 새 데이터를 RAWDATA_DIR 에 넣은 뒤, 이 스크립트 하나로 Supabase 최신화.

실행:
  .venv/bin/python data_pipeline/run_pipeline.py            # 전체 (가격→종목→신호) : 새 재무 들어왔을 때
  .venv/bin/python data_pipeline/run_pipeline.py --prices   # 가격만 : 매일용 (빠름)

각 단계는 별도 프로세스로 실행하고, 하나라도 실패하면 즉시 멈춘다(데이터 꼬임 방지).
"""
import argparse
import subprocess
import sys
import time

ALL_STEPS = [
    ("가격 적재(prices)",  "data_pipeline/load_prices.py"),
    ("종목 보강(stocks)",  "data_pipeline/load_stocks.py"),
    ("신호 계산(signals)", "data_pipeline/compute_signals.py"),
]
PRICES_ONLY = ALL_STEPS[:1]


def run(steps):
    t0 = time.time()
    for i, (name, script) in enumerate(steps, 1):
        print(f"\n[{i}/{len(steps)}] ▶ {name}  ({script})", flush=True)
        if subprocess.run([sys.executable, script]).returncode != 0:
            print(f"\n✗ 실패: {name} — 파이프라인 중단 (이후 단계는 실행 안 함)")
            sys.exit(1)
    print(f"\n✓ 파이프라인 완료 — {len(steps)}단계, {time.time() - t0:.0f}s")


def main():
    ap = argparse.ArgumentParser(description="서서히 데이터 갱신 파이프라인")
    ap.add_argument("--prices", action="store_true",
                    help="가격만 갱신(매일용). 지정 안 하면 전체 실행.")
    args = ap.parse_args()
    run(PRICES_ONLY if args.prices else ALL_STEPS)


if __name__ == "__main__":
    main()
