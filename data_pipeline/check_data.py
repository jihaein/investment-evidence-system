"""
check_data.py — 적재 후 데이터 건전성 점검. '오래된/깨진 데이터'를 앱에 내보내기 전에 잡는다.
실행:  .venv/bin/python data_pipeline/check_data.py
      (run_pipeline.py 가 마지막 단계로 자동 호출)

판정:
  · 구조 문제(행 수 붕괴, 데이터 없음) → 치명적 → 종료코드 1 (스케줄러가 '실패'로 인지)
  · 신선도 문제(가격이 오래됨) → 경고만 (라이브 피드 연결 전이면 정상)
      → 라이브 피드 붙인 뒤엔 FAIL_ON_STALE=1 로 바꿔 '오래되면 실패'로 격상 권장.
"""
import os
from datetime import date
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv("/Users/jihaein/Developer/investment-evidence-system/.env")
engine = create_engine(os.environ["SUPABASE_DB_URL"])

# 기준값 (현재 데이터 기준 — 데이터가 늘면 올려라)
MIN_STOCKS  = 100
MIN_PRICES  = 150_000
MIN_SIGNALS = 30_000
MAX_PRICE_STALE_DAYS = 10            # 최신 가격일이 이보다 오래되면 '오래됨'
FAIL_ON_STALE = os.getenv("FAIL_ON_STALE") == "1"   # 라이브 피드 연결 후 1 로


def main():
    fatal, warn = [], []
    with engine.connect() as con:
        ns  = con.execute(text("select count(*) from stocks")).scalar()
        npx = con.execute(text("select count(*) from prices")).scalar()
        nsg = con.execute(text("select count(*) from signals")).scalar()
        last_price = con.execute(text("select max(bas_dd) from prices")).scalar()
        ndup = con.execute(text("select count(*) from signals where dupont_score is null")).scalar()

    print(f"stocks={ns}  prices={npx:,}  signals={nsg:,}  최신가격일={last_price}")

    # 구조(치명적)
    if ns  < MIN_STOCKS:  fatal.append(f"종목 수 비정상: {ns} < {MIN_STOCKS}")
    if npx < MIN_PRICES:  fatal.append(f"가격 행 비정상: {npx:,} < {MIN_PRICES:,}")
    if nsg < MIN_SIGNALS: fatal.append(f"신호 행 비정상: {nsg:,} < {MIN_SIGNALS:,}")
    if last_price is None: fatal.append("가격 데이터 없음")
    if nsg and ndup / nsg > 0.20:
        warn.append(f"DuPont null 비율 급증: {ndup/nsg:.0%}")

    # 신선도
    if last_price is not None:
        stale = (date.today() - last_price).days
        if stale > MAX_PRICE_STALE_DAYS:
            msg = f"가격이 오래됨: 최신 {last_price} ({stale}일 전) — 갱신 안 됐을 수 있음"
            (fatal if FAIL_ON_STALE else warn).append(msg)

    for w in warn:  print("  ⚠️ ", w)
    for f in fatal: print("  ✗ ", f)

    if fatal:
        print("\n✗ 건전성 점검 실패 — 앱에 내보내기 전에 확인 필요")
        raise SystemExit(1)
    print("\n✓ 데이터 건전성 OK" + ("  (경고 있음)" if warn else ""))


if __name__ == "__main__":
    main()
