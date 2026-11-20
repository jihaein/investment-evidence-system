"""
fetch_disclosures.py — DART 공시검색(list.json)으로 정기보고서의 '실제 공시일'을 수집.
preprocess_metadata.json 의 disclosed_date 원천(접수번호 앞자리로 날짜 추정 금지 규칙 충족).
정기공시(pblntf_ty=A: 사업·반기·분기보고서)만. 빠르다(종목당 보통 1~2페이지).

실행:  .venv/bin/python data_pipeline/fetch/fetch_disclosures.py
       (최신화하려면 _raw_pull/dart/list/ 를 지우고 다시 실행 — 기본은 이어받기)
출력:  _raw_pull/dart/list/{corp}.json  (rcept_no·rcept_dt·report_nm 목록)
"""
import os, csv, json, time
from pathlib import Path
from datetime import date
import requests
from dotenv import load_dotenv

ROOT = "/Users/jihaein/Developer/investment-evidence-system"
load_dotenv(f"{ROOT}/.env")
KEY    = os.environ["OPENDART_API_KEY"]
MASTER = f"{ROOT}/data/rawdata/12_기업개황_KSIC.csv"
OUTDIR = Path(os.environ["RAWDATA_DIR"]) / "_raw_pull" / "dart" / "list"
URL    = "https://opendart.fss.or.kr/api/list.json"
BGN    = "20190101"

def load_corps():
    with open(MASTER, encoding="utf-8-sig") as f:
        return [r[0] for r in list(csv.reader(f))[2:]]

def fetch_corp(corp, end):
    out, page = [], 1
    while True:
        p = {"crtfc_key": KEY, "corp_code": corp, "bgn_de": BGN, "end_de": end,
             "pblntf_ty": "A", "page_no": page, "page_count": 100}
        d = requests.get(URL, params=p, timeout=20).json()
        st = d.get("status")
        if st == "013":                      # 조회 데이터 없음
            break
        if st != "000":
            print(f"  ? {corp} p{page}: status {st}"); break
        out += d.get("list", [])
        if page >= int(d.get("total_page", 1)):
            break
        page += 1; time.sleep(0.15)
    return out

def main():
    end = date.today().strftime("%Y%m%d")
    OUTDIR.mkdir(parents=True, exist_ok=True)
    corps = load_corps(); tot = 0
    for i, corp in enumerate(corps, 1):
        f = OUTDIR / f"{corp}.json"
        if f.exists():                       # 이어받기
            continue
        lst = fetch_corp(corp, end)
        f.write_text(json.dumps({"list": lst}, ensure_ascii=False), encoding="utf-8")
        tot += len(lst)
        print(f"[{i}/{len(corps)}] {corp} — {len(lst)}건", end="\r")
        time.sleep(0.15)
    print(f"\n완료: 정기공시 {tot}건 저장 → {OUTDIR}")

if __name__ == "__main__":
    main()
