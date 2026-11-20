"""DART 기업개황(company) 원본 수집기 — 120종목. 회사당 1건. 계약서 12_기업개황 재료."""
import os, csv, json, time, requests
from pathlib import Path
from dotenv import load_dotenv

ROOT = str(Path(__file__).resolve().parents[2])
load_dotenv(f"{ROOT}/.env")
KEY = os.environ["OPENDART_API_KEY"]
MASTER = Path(os.environ["RAWDATA_DIR"]) / "12_기업개황_KSIC.csv"
OUTDIR = Path(os.environ["RAWDATA_DIR"]) / "_raw_pull/dart/company"
URL = "https://opendart.fss.or.kr/api/company.json"

def load_corps():
    with open(MASTER, encoding="utf-8-sig") as fh:
        return [(r[0], r[1]) for r in list(csv.reader(fh))[2:]]   # (corp_code, corp_name)

def main():
    OUTDIR.mkdir(parents=True, exist_ok=True)
    corps = load_corps(); got = empty = err = 0
    for i, (corp, name) in enumerate(corps, 1):
        f = OUTDIR / f"{corp}.json"
        if f.exists(): continue
        d = requests.get(URL, params={"crtfc_key": KEY, "corp_code": corp}, timeout=20).json()
        st = d.get("status")
        if st == "000": f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8"); got += 1
        elif st == "013": empty += 1
        else: err += 1; print(f"  ? {corp} {name}: status {st}")
        time.sleep(0.2); print(f"[{i}/{len(corps)}] {name}", end="\r")
    print(f"\n완료: 받음 {got} 없음 {empty} 오류 {err}\n저장: {OUTDIR}")

if __name__ == "__main__":
    main()