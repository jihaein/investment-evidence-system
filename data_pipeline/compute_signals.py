"""
종목별 (매수기준일 × 평가일) 조합의 서서히 점수와 판정을 계산해 signals 테이블에 upsert한다.
입력: RAWDATA_DIR 의 기준일별 스냅샷
실행: .venv/bin/python data_pipeline/compute_signals.py
"""
import os, sys
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

sys.path.insert(0, "/Users/jihaein/Developer/investment-evidence-system")  # models/ 임포트용
from models.seoseohi.pipeline import load_data_pit, run_seoseohi_pit, integrate, integrate_sum
from models.seoseohi.fscore import list_snapshot_dates

load_dotenv("/Users/jihaein/Developer/investment-evidence-system/.env")
DB_URL = os.environ["SUPABASE_DB_URL"]
RAWDATA_DIR = os.environ["RAWDATA_DIR"]


def compute(base_dir, stock_codes):
    data  = load_data_pit(base_dir)
    snaps = sorted(list_snapshot_dates(base_dir), key=lambda d: pd.Timestamp(d))
    rows = []
    for i, s in enumerate(stock_codes, 1):
        for bi, baseline in enumerate(snaps):
            for ev in snaps[bi + 1:]:          # 평가일은 매수기준선보다 뒤
                sc = run_seoseohi_pit(data, {"stock_code": s, "buy_date": baseline}, ev)["scores"]
                valid = [int(v) for v in sc.values() if not pd.isna(v)]
                rows.append({
                    "stock_code": s, "baseline_date": baseline, "eval_date": ev,
                    "dupont_score": None if pd.isna(sc["dupont"]) else int(sc["dupont"]),
                    "ohlson_score": None if pd.isna(sc["ohlson"]) else int(sc["ohlson"]),
                    "fscore":       None if pd.isna(sc["fscore"]) else int(sc["fscore"]),
                    "sum_total":    sum(valid) if valid else None,
                    "verdict_count": integrate(sc),
                    "verdict_sum":   integrate_sum(sc),
                })
        print(f"  {i}/{len(stock_codes)} {s}", end="\r")
    print()
    df = pd.DataFrame(rows)
    for c in ["dupont_score", "ohlson_score", "fscore", "sum_total"]:
        df[c] = df[c].astype("Int64")                       # NaN→정수(널 허용)
    df["baseline_date"] = pd.to_datetime(df["baseline_date"]).dt.date   # 문자열→날짜
    df["eval_date"]     = pd.to_datetime(df["eval_date"]).dt.date
    return df


def main():
    engine = create_engine(DB_URL)
    codes = pd.read_sql("select stock_code from stocks order by stock_code",
                        engine)["stock_code"].tolist()
    print(f"종목 {len(codes)}개 — 전체 (매수기준선 × 평가일) 매트릭스 계산 시작")
    df = compute(RAWDATA_DIR, codes)
    df.to_pickle("data_pipeline/_signals_cache.pkl")        # 재계산 대비 캐시
    # 스테이징 테이블에 올린 뒤 upsert
    with engine.begin() as con:
        df.to_sql("_stg_signals", con, if_exists="replace", index=False,
                  method="multi", chunksize=1000)
        con.execute(text("""
            insert into signals (stock_code, baseline_date, eval_date, dupont_score,
                                 ohlson_score, fscore, sum_total, verdict_count, verdict_sum)
            select stock_code, baseline_date, eval_date, dupont_score,
                   ohlson_score, fscore, sum_total, verdict_count, verdict_sum from _stg_signals
            on conflict (stock_code, baseline_date, eval_date) do update set
                dupont_score=excluded.dupont_score, ohlson_score=excluded.ohlson_score,
                fscore=excluded.fscore, sum_total=excluded.sum_total,
                verdict_count=excluded.verdict_count, verdict_sum=excluded.verdict_sum
        """))
        con.execute(text("drop table _stg_signals"))
    with engine.connect() as con:
        n     = con.execute(text("select count(*) from signals")).scalar()
        sells = con.execute(text("select count(*) from signals where verdict_sum='Yes(훼손 우려)'")).scalar()
    print(f"signals 적재 완료: {n:,}행, 합산 매도신호 {sells}건")


if __name__ == "__main__":
    main()
