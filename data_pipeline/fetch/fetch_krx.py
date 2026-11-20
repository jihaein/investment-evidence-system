"""
KRX 일별 시세(종가·시가총액·상장주식수)를 받아 시장자료 CSV로 저장한다.
pykrx 로그인 버전을 쓰며 KRX_ID/KRX_PW 는 .env 에서 읽는다.
실행: .venv/bin/python data_pipeline/fetch/fetch_krx.py [--date YYYYMMDD | --from YYYYMMDD --to YYYYMMDD]
"""
import os, csv, argparse
from pathlib import Path
from dotenv import load_dotenv

ROOT = "/Users/jihaein/Developer/investment-evidence-system"
load_dotenv(f"{ROOT}/.env")                 # pykrx 임포트 전에 읽어야 로그인됨
import pandas as pd
from pykrx import stock

RAWDATA_DIR = os.environ["RAWDATA_DIR"]
MASTER = f"{ROOT}/data/rawdata/12_기업개황_KSIC.csv"
OUT = Path(RAWDATA_DIR) / "추가자료" / "시장자료" / "krx_market.csv"
CLS_TO_MKT = {"K": "코스닥", "Y": "유가증권", "E": "코넥스"}

def load_master():
    with open(MASTER, encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))[2:]        # 헤더 2줄 건너뜀
    # stock_code → (stock_name, market).  열: code=2, name=6, corp_cls=7
    return {r[2]: (r[6], CLS_TO_MKT.get(r[7], "코스닥")) for r in rows}

def pull_day(date, master):
    df = stock.get_market_cap_by_ticker(date, market="ALL")
    if df.empty or (df[["종가", "시가총액"]] == 0).all(axis=None):
        return []                              # 휴장일
    out = []
    for ticker, row in df.iterrows():
        if ticker not in master:               # 우리 120종목만
            continue
        name, mkt = master[ticker]
        out.append({"stock_code": ticker, "stock_name_2014": name, "MKT_NM": mkt,
                    "BAS_DD": date, "MKTCAP": int(row["시가총액"]),
                    "LIST_SHRS": int(row["상장주식수"]), "TDD_CLSPRC": int(row["종가"]),
                    "quality_status": "OK"})
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date"); ap.add_argument("--from", dest="frm"); ap.add_argument("--to")
    a = ap.parse_args()
    master = load_master()
    if a.frm and a.to:
        dates = [d.strftime("%Y%m%d") for d in pd.bdate_range(a.frm, a.to)]
    elif a.date:
        dates = [a.date]
    else:
        dates = [stock.get_nearest_business_day_in_a_week()]   # 최근 거래일

    rows = []
    for d in dates:
        day = pull_day(d, master); print(f"  {d}: {len(day)}종목"); rows += day
    if not rows:
        print("받은 데이터 없음(휴장일?) — 파일 안 씀"); return

    OUT.parent.mkdir(parents=True, exist_ok=True)
    cols = ["stock_code", "stock_name_2014", "MKT_NM", "BAS_DD",
            "MKTCAP", "LIST_SHRS", "TDD_CLSPRC", "quality_status"]
    pd.DataFrame(rows)[cols].to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"\n{len(rows)}행 저장: {OUT}")

if __name__ == "__main__":
    main()