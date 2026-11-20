"""
적재 후 데이터 점검.
행 수가 기준보다 적으면 실패(종료코드 1), 시세가 오래됐으면 경고만 한다.
FAIL_ON_STALE=1 이면 오래된 시세도 실패로 처리한다.
실행: .venv/bin/python data_pipeline/check_data.py
"""
import os
from datetime import date
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv("/Users/jihaein/Developer/investment-evidence-system/.env")
engine = create_engine(os.environ["SUPABASE_DB_URL"])

# 최소 기준값
MIN_STOCKS  = 100
MIN_PRICES  = 150_000
MIN_SIGNALS = 30_000
MAX_PRICE_STALE_DAYS = 10            # 최신 가격일이 이보다 오래되면 '오래됨'
FAIL_ON_STALE = os.getenv("FAIL_ON_STALE") == "1"


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

    for w in warn:  print("  [경고]", w)
    for f in fatal: print("  [실패]", f)

    if fatal:
        print("\n점검 실패")
        raise SystemExit(1)
    print("\n점검 통과" + ("  (경고 있음)" if warn else ""))


if __name__ == "__main__":
    main()
