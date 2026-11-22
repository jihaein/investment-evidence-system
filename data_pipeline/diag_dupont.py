"""
diag_dupont.py — 불일치 사례에서 스냅샷 vs 플랫이 '어떤 행/중위수'를 고르는지 비교.
실행: .venv/bin/python data_pipeline/diag_dupont.py
"""
import os, sys
import pandas as pd
ROOT = str(__import__("pathlib").Path(__file__).resolve().parents[1])  # repo root, any computer
sys.path.insert(0, ROOT)
from dotenv import load_dotenv
load_dotenv(f"{ROOT}/.env")
from models.seoseohi.dupont import (
    build_dupont_table_from_snapshot, dp_resolve_snapshot_dir, dp_latest_asof,
    dp_industry_medians, load_ksic_from_base, DP_NUMERIC, DP_MERGE_KEYS)

RD = os.environ["RAWDATA_DIR"]
ksic = load_ksic_from_base(RD)
KMAP = ksic.set_index("stock_code")["ksic_mid_2digit"].to_dict()

def build_flat_table(flat_dir):
    ttm   = pd.read_csv(f"{flat_dir}/11_재무_DuPont_TTM_2019-2026.csv",  skiprows=[1], dtype=str, encoding="utf-8-sig")
    dates = pd.read_csv(f"{flat_dir}/11B_DuPont_날짜연결_2019-2026.csv", skiprows=[1], dtype=str, encoding="utf-8-sig")
    ttm["stock_code"]=ttm["stock_code"].str.zfill(6); dates["stock_code"]=dates["stock_code"].str.zfill(6)
    for c in DP_NUMERIC: ttm[c]=pd.to_numeric(ttm[c],errors="coerce")
    m = ttm.merge(dates[DP_MERGE_KEYS+["disclosed_date","available_from","period_end"]], on=DP_MERGE_KEYS, how="left")
    af = pd.to_datetime(m["available_from"], format="%Y%m%d", errors="coerce")
    m["disclosed_date"] = af.fillna(pd.to_datetime(m["disclosed_date"], errors="coerce"))
    return m.merge(ksic, on="stock_code", how="left")

flat = build_flat_table(RD)

def show_row(tag, row):
    if row is None: print(f"    {tag}: (없음)"); return
    print(f"    {tag}: 공시일(컷)={str(row['disclosed_date'])[:10]} rcept={row.get('rcept_no')} "
          f"PM={row['PM_pct']} ATO={row['ATO']} bsns={row.get('bsns_year')} q={row.get('quarter')}")

for s in ["027580", "112240", "010470"]:
    code = KMAP.get(s)
    print(f"\n==== {s} (업종 {code}) ====")
    for asof in ["2023-04-01", "2026-09-01"]:
        print(f"  [{asof}]")
        sdir = dp_resolve_snapshot_dir(RD, asof)
        st = build_dupont_table_from_snapshot(sdir, ksic)
        show_row("SNAP", dp_latest_asof(st, s, asof))
        show_row("FLAT", dp_latest_asof(flat, s, asof))
        sm = dp_industry_medians(st, asof, code); fm = dp_industry_medians(flat, asof, code)
        print(f"    SNAP 중위수 PM={sm['PM_median']} ATO={sm['ATO_median']} (n_pm={sm['n_pm']})")
        print(f"    FLAT 중위수 PM={fm['PM_median']} ATO={fm['ATO_median']} (n_pm={fm['n_pm']})")
