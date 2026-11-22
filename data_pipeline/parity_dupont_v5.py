"""
parity_dupont_v5.py — DuPont 플랫 vs 스냅샷. 시점 컷 = disclosed_date(스냅샷 구성 규칙과 동일).
넓은 매수일 범위로 돌리고, 남는 불일치의 '매수일 분포'를 본다.
실행: .venv/bin/python data_pipeline/parity_dupont_v5.py
"""
import os, sys
import pandas as pd
ROOT = str(__import__("pathlib").Path(__file__).resolve().parents[1])  # repo root, any computer
sys.path.insert(0, ROOT)
from dotenv import load_dotenv
load_dotenv(f"{ROOT}/.env")
from models.seoseohi.dupont import (
    dupont_compare_pit, load_ksic_from_base, dp_list_snapshot_dates,
    dp_latest_asof, dp_industry_medians, score_dupont, DP_NUMERIC, DP_MERGE_KEYS,
    DUPONT_SELF_MEDIAN_OWN_CHANGE, _is_self_median)

RD = os.environ["RAWDATA_DIR"]

def build_flat_table(flat_dir, ksic):
    ttm   = pd.read_csv(f"{flat_dir}/11_재무_DuPont_TTM_2019-2026.csv",  skiprows=[1], dtype=str, encoding="utf-8-sig")
    dates = pd.read_csv(f"{flat_dir}/11B_DuPont_날짜연결_2019-2026.csv", skiprows=[1], dtype=str, encoding="utf-8-sig")
    ttm["stock_code"]=ttm["stock_code"].str.zfill(6); dates["stock_code"]=dates["stock_code"].str.zfill(6)
    for c in DP_NUMERIC: ttm[c]=pd.to_numeric(ttm[c],errors="coerce")
    m = ttm.merge(dates[DP_MERGE_KEYS+["disclosed_date","available_from","period_end"]], on=DP_MERGE_KEYS, how="left")
    m["disclosed_date"] = pd.to_datetime(m["disclosed_date"], errors="coerce")   # ★ disclosed_date 컷
    return m.merge(ksic, on="stock_code", how="left")

def _ratio(num, den):
    return None if den in (None,0) or pd.isna(den) else num/den

def flat_score(table, ksic, stock, buy, now):
    b = dp_latest_asof(table, stock, buy); n = dp_latest_asof(table, stock, now)
    code = ksic.set_index("stock_code")["ksic_mid_2digit"].get(stock)
    if b is None or n is None or code is None: return None
    mb = dp_industry_medians(table, buy, code); mn = dp_industry_medians(table, now, code)
    pm_own  = DUPONT_SELF_MEDIAN_OWN_CHANGE and _is_self_median(b, mb, "PM_pct")
    ato_own = DUPONT_SELF_MEDIAN_OWN_CHANGE and _is_self_median(b, mb, "ATO")
    dpm=dato=None
    if pm_own and pd.notna(b["PM_pct"]) and pd.notna(n["PM_pct"]): dpm = n["PM_pct"]-b["PM_pct"]
    elif pd.notna(b["PM_pct"]) and pd.notna(n["PM_pct"]) and pd.notna(mb["PM_median"]) and pd.notna(mn["PM_median"]):
        dpm = (n["PM_pct"]-mn["PM_median"]) - (b["PM_pct"]-mb["PM_median"])
    if ato_own and pd.notna(b["ATO"]) and pd.notna(n["ATO"]) and b["ATO"]!=0: dato=(n["ATO"]/b["ATO"]-1)*100
    elif pd.notna(b["ATO"]) and pd.notna(n["ATO"]):
        rb=_ratio(b["ATO"],mb["ATO_median"]); rn=_ratio(n["ATO"],mn["ATO_median"])
        if rb not in (None,0) and rn is not None: dato=(rn/rb-1)*100
    return score_dupont(dpm,dato)

def main():
    ksic = load_ksic_from_base(RD)
    table = build_flat_table(RD, ksic)
    snaps = dp_list_snapshot_dates(RD)
    stocks = ksic["stock_code"].tolist()
    pairs = [(snaps[i], snaps[-1]) for i in range(1, len(snaps)-1, 1)]   # 모든 매수일(2020-04~) → 최신
    same=diff=skip=0; mism=[]
    for s in stocks:
        for buy, now in pairs:
            sc = dupont_compare_pit(RD, ksic, s, buy, now)["score"]
            fc = flat_score(table, ksic, s, buy, now)
            if sc is None and fc is None: skip+=1
            elif sc==fc: same+=1
            else: diff+=1; mism.append((s,str(buy)[:10],sc,fc))
    print(f"\n일치 {same} | 불일치 {diff} | 둘다없음 {skip}  (표본 {len(stocks)}종목 × {len(pairs)}쌍)")
    if diff:
        from collections import Counter
        print("불일치 매수일 분포:", dict(sorted(Counter(m[1] for m in mism).items())))
        for m in mism[:12]: print("  ",m)
    else:
        print("✓ 모든 매수일에서 완전 일치")

if __name__ == "__main__":
    main()
