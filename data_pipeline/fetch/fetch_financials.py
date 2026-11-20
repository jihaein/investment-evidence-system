"""
DART 전체 재무제표(fnlttSinglAcntAll) 원본 수집.
120종목 × 연도 × 보고서 × 연결/별도 응답을 JSON 그대로 저장한다. 이미 받은 파일은 건너뛴다.
실행: .venv/bin/python data_pipeline/fetch/fetch_financials.py --years 2019-2026
"""
import os, csv, json, time, argparse
import requests
from pathlib import Path
from dotenv import load_dotenv

ROOT = "/Users/jihaein/Developer/investment-evidence-system"
load_dotenv(f"{ROOT}/.env")
KEY = os.environ["OPENDART_API_KEY"]

MASTER  = f"{ROOT}/data/rawdata/12_기업개황_KSIC.csv"
OUTDIR  = Path(ROOT) / "data/rawdata/_raw_pull/dart/fnlttSinglAcntAll"
URL     = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
REPRTS  = ["11013", "11012", "11014", "11011"]   # 1분기·반기·3분기·사업보고서
FS_DIVS = ["CFS", "OFS"]
SLEEP   = 0.2

def load_corps(limit=None):
    with open(MASTER, encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))[2:]           # 헤더 2줄 건너뜀
    out = [(r[0], r[2], r[1]) for r in rows]      # (corp_code, stock_code, corp_name)
    return out[:limit] if limit else out

def fetch_one(corp, year, reprt, fs):
    p = {"crtfc_key": KEY, "corp_code": corp, "bsns_year": str(year),
         "reprt_code": reprt, "fs_div": fs}
    return requests.get(URL, params=p, timeout=20).json()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2019-2026")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    parts = args.years.split("-")
    years = range(int(parts[0]), int(parts[-1]) + 1)

    corps = load_corps(args.limit)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    got = skip = empty = err = 0
    for ci, (corp, stock, name) in enumerate(corps, 1):
        for year in years:
            for reprt in REPRTS:
                for fs in FS_DIVS:
                    f = OUTDIR / f"{corp}_{year}_{reprt}_{fs}.json"
                    if f.exists():                       # 이어받기
                        skip += 1; continue
                    try:
                        d = fetch_one(corp, year, reprt, fs)
                    except Exception as e:
                        err += 1; print(f"  ! {corp} {year} {reprt} {fs}: {e}"); continue
                    st = d.get("status")
                    if st == "000":
                        f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8"); got += 1
                    elif st == "013":
                        empty += 1                        # 그 조합은 보고서 없음(정상)
                    elif st == "020":
                        print("일일 사용한도 초과 — 중단"); 
                        print(f"받음 {got} 건너뜀 {skip} 없음 {empty} 오류 {err}"); return
                    else:
                        err += 1; print(f"  ? {corp} {year} {reprt} {fs}: status {st}")
                    time.sleep(SLEEP)
        print(f"[{ci}/{len(corps)}] {name} — 누적 받음 {got} 없음 {empty} 오류 {err}", flush=True)
    print(f"\n완료: 받음 {got} 건너뜀 {skip} 없음 {empty} 오류 {err}\n저장 위치: {OUTDIR}")

if __name__ == "__main__":
    main()