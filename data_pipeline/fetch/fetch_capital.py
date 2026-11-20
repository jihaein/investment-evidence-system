"""
fetch_capital.py — DART 증자(감자)현황(irdsSttus) 원본 수집기. 계약서 09 재료.
120종목 × 연도 × 보고서. ★ API 응답에 stock_code 가 없으므로 corp_code→stock_code 를
각 항목에 주입해 저장한다(데이터팀 모듈이 stock_code 로 마스터와 대조하기 때문).
실행: .venv/bin/python data_pipeline/fetch/fetch_capital.py --years 2019-2026
"""
import os, csv, json, time, argparse, requests
from pathlib import Path
from dotenv import load_dotenv

ROOT = "/Users/jihaein/Developer/investment-evidence-system"
load_dotenv(f"{ROOT}/.env")
KEY = os.environ["OPENDART_API_KEY"]
MASTER = f"{ROOT}/data/rawdata/12_기업개황_KSIC.csv"
OUTDIR = Path(os.environ["RAWDATA_DIR"]) / "_raw_pull" / "dart" / "irdsSttus"
URL = "https://opendart.fss.or.kr/api/irdsSttus.json"
REPRTS = ["11013", "11012", "11014", "11011"]

def load_corps(limit=None):
    with open(MASTER, encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))[2:]
    out = [(r[0], r[2], r[1]) for r in rows]   # (corp_code, stock_code, corp_name)
    return out[:limit] if limit else out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2019-2026"); ap.add_argument("--limit", type=int)
    a = ap.parse_args(); p = a.years.split("-"); years = range(int(p[0]), int(p[-1]) + 1)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    corps = load_corps(a.limit); got = empty = err = 0
    for ci, (corp, stock, name) in enumerate(corps, 1):
        for year in years:
            for reprt in REPRTS:
                f = OUTDIR / f"{corp}_{year}_{reprt}.json"
                if f.exists():
                    continue
                d = requests.get(URL, params={"crtfc_key": KEY, "corp_code": corp,
                    "bsns_year": str(year), "reprt_code": reprt}, timeout=20).json()
                st = d.get("status")
                if st == "000":
                    for it in d.get("list", []):       # ★ 응답에 없는 stock_code 주입
                        it["stock_code"] = stock
                    f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8"); got += 1
                elif st == "013":
                    empty += 1
                elif st == "020":
                    print("일일 한도 초과 — 중단"); return
                else:
                    err += 1
                time.sleep(0.2)
        print(f"[{ci}/{len(corps)}] {name} — 받음 {got} 없음 {empty}", flush=True)
    print(f"\n완료: 받음 {got} 없음 {empty} 오류 {err}\n저장: {OUTDIR}")

if __name__ == "__main__":
    main()
