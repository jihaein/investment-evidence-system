"""
build_metadata.py — preprocess_metadata.json 생성 (데이터팀 preprocess() 입력).
원천: 수집한 재무 원본(rcept_no) + 공시검색(공시일) + 기존 계약 CSV(공시일·기말일 보완)
      + 마스터(결산월) + 시세(거래일). 네트워크 불필요 — fetch 결과와 기존 데이터만 읽음.

공시일 우선순위: ① 공시검색(list.json, 최신·전체) ② 기존 계약 CSV(과거분 보완)
기말일: 기존 CSV 값 우선, 없으면 보고서코드+결산월로 계산.
실행:  .venv/bin/python data_pipeline/build_metadata.py
출력:  _raw_pull/preprocess_metadata.json
"""
import os, json, glob, csv, calendar
from pathlib import Path
from dotenv import load_dotenv

ROOT = str(Path(__file__).resolve().parents[1])
load_dotenv(f"{ROOT}/.env")
RD  = os.environ["RAWDATA_DIR"]
RAW = os.path.join(RD, "_raw_pull")
FIN = os.path.join(RAW, "dart", "fnlttSinglAcntAll")

def last_day(y, m):
    return f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"

def calc_period_end(fiscal_year, acc_mt, reprt):
    """결산월(acc_mt)과 보고서코드로 분기말 계산. 비12월 결산도 처리."""
    em = int(acc_mt or 12)
    before = {"11011": 0, "11014": 3, "11012": 6, "11013": 9}[reprt]
    m, y = em - before, fiscal_year
    while m <= 0:
        m += 12; y -= 1
    return last_day(y, m)

def read_two(path):
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    h = rows[0]
    return [dict(zip(h, r)) for r in rows[2:]]

def main():
    # 1) 마스터 결산월
    acc = {r["corp_code"]: (r.get("acc_mt") or "12") for r in read_two(f"{RD}/12_기업개황_KSIC.csv")}

    # 2) 공시일 맵: ① 공시검색(우선) ② 기존 계약 CSV(보완)
    dates, pends = {}, {}
    for p in glob.glob(f"{RAW}/dart/list/*.json"):          # ① 공시검색
        for it in json.load(open(p, encoding="utf-8")).get("list", []):
            rc, dt = it.get("rcept_no"), it.get("rcept_dt")
            if rc and dt and len(dt) == 8:
                dates.setdefault(rc, f"{dt[:4]}-{dt[4:6]}-{dt[6:]}")
    for p in sorted(glob.glob(f"{RAW}/dart/receipts/*.json")):
        with open(p, encoding="utf-8") as handle:
            it = json.load(handle)
        rc, dt = it["rcept_no"], it["rcept_dt"]
        dates[rc] = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"
    for fn in ["04_재무_Ohlson용_2019-2026.csv", "11B_DuPont_날짜연결_2019-2026.csv",
               "02_재무_연간_2017-2026.csv"]:                # ② 기존 CSV 보완
        p = f"{RD}/{fn}"
        if os.path.exists(p):
            for r in read_two(p):
                rc = r.get("rcept_no")
                if rc:
                    if r.get("disclosed_date"): dates.setdefault(rc, r["disclosed_date"])
                    if r.get("period_end"):     pends.setdefault(rc, r["period_end"])

    # 3) 원본 재무: (corp,year,reprt) 그룹별 rcept_no + CFS/OFS 존재
    groups = {}
    for path in glob.glob(f"{FIN}/*.json"):
        parts = os.path.basename(path)[:-5].split("_")
        if len(parts) != 4:
            continue
        corp, year, reprt, fs = parts
        with open(path, encoding="utf-8") as handle:
            if json.load(handle).get("status") != "000":
                continue
        g = groups.setdefault((corp, year, reprt), {"rcept": None, "cfs": False, "ofs": False})
        g["cfs" if fs == "CFS" else "ofs"] = True
        if g["rcept"] is None:
            try:
                lst = (json.load(open(path, encoding="utf-8")).get("list") or [])
            except Exception:
                lst = []
            if lst:
                g["rcept"] = lst[0].get("rcept_no")

    # 4) filings 조립
    filings, missing = {}, []
    for (corp, year, reprt), g in groups.items():
        rc = g["rcept"]
        if not rc:
            continue
        dd = dates.get(rc)
        if not dd:
            missing.append((corp, year, reprt)); continue
        fy = int(year)
        pe = pends.get(rc) or calc_period_end(fy, acc.get(corp, "12"), reprt)
        e = {"corp_code": corp, "bsns_year": int(year), "reprt_code": reprt,
             "fiscal_year": fy, "disclosed_date": dd, "period_end": pe, "required": True}
        if g["ofs"] and not g["cfs"]:
            cfs = os.path.join(FIN, f"{corp}_{year}_{reprt}_CFS.json")
            if os.path.exists(cfs):
                with open(cfs, encoding="utf-8") as handle:
                    e["cfs_absent_verified"] = json.load(handle).get("status") == "013"
        filings[rc] = e

    # 5) trading_days = 시세 BAS_DD
    tdays = set()
    for p in glob.glob(f"{RD}/추가자료/시장자료/*.csv"):
        with open(p, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                b = row.get("BAS_DD")
                if b and len(b) == 8:
                    tdays.add(f"{b[:4]}-{b[4:6]}-{b[6:]}")

    meta = {"filings": filings, "trading_days": sorted(tdays), "verified_facts": []}
    out = os.path.join(RAW, "preprocess_metadata.json")
    json.dump(meta, open(out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"filings {len(filings)} | 거래일 {len(meta['trading_days'])} | 공시일 없어 빠진 그룹 {len(missing)}")
    if missing:
        from collections import Counter
        print("  빠진 그룹 연도별:", dict(Counter(m[1] for m in missing)))
        print("  → 공시검색(fetch_disclosures)을 먼저 돌리면 대부분 채워집니다.")
    print(f"저장: {out}")

if __name__ == "__main__":
    main()
