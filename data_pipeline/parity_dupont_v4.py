"""
parity_dupont.py — DuPont 신호: '스냅샷 방식'(현재) vs '플랫 CSV 방식'(신규) 점수 일치 검증.
스냅샷은 available_from(공시 다음 거래일) 기준으로 시점 컷 → 플랫도 available_from 으로 필터해야 일치.
같은 원본(data/rawdata)으로 두 방식을 돌려 점수가 같은지 본다. 같으면 플랫 전환 안전.
실행: .venv/bin/python data_pipeline/parity_dupont.py
"""
import os, sys
import pandas as pd
from dotenv import load_dotenv

ROOT = "/Users/jihaein/Developer/investment-evidence-system"
sys.path.insert(0, ROOT)
load_dotenv(f"{ROOT}/.env")
from models.seoseohi.dupont import (
    dupont_compare_pit, load_ksic_from_base, dp_list_snapshot_dates,
    dp_latest_asof, dp_industry_medians, score_dupont, DP_NUMERIC, DP_MERGE_KEYS,
    DUPONT_SELF_MEDIAN_OWN_CHANGE, _is_self_median)

RD = os.environ["RAWDATA_DIR"]

def build_flat_table(flat_dir, ksic):
    ttm   = pd.read_csv(f"{flat_dir}/11_재무_DuPont_TTM_2019-2026.csv",  skiprows=[1], dtype=str, encoding="utf-8-sig")
    dates = pd.read_csv(f"{flat_dir}/11B_DuPont_날짜연결_2019-2026.csv", skiprows=[1], dtype=str, encoding="utf-8-sig")
    ttm["stock_code"]   = ttm["stock_code"].str.zfill(6)
    dates["stock_code"] = dates["stock_code"].str.zfill(6)
    for c in DP_NUMERIC:
        ttm[c] = pd.to_numeric(ttm[c], errors="coerce")
    m = ttm.merge(dates[DP_MERGE_KEYS + ["disclosed_date", "available_from", "period_end"]],
                  on=DP_MERGE_KEYS, how="left")
    # ★ 시점 컷 기준 = available_from(공시 다음 거래일). 라이브 함수들이 'disclosed_date' 컬럼으로
    #    필터하므로, 그 컬럼에 available_from 을 넣어 스냅샷과 같은 시점 컷을 재현한다.
    af = pd.to_datetime(m["available_from"], format="%Y%m%d", errors="coerce")
    m["disclosed_date"] = af.fillna(pd.to_datetime(m["disclosed_date"], errors="coerce"))
    return m.merge(ksic, on="stock_code", how="left")

def _safe_ratio(num, den):
    return None if den in (None, 0) or pd.isna(den) else num / den

def dupont_flat_score(table, ksic, stock, buy, now):
    b = dp_latest_asof(table, stock, buy)
    n = dp_latest_asof(table, stock, now)
    code = ksic.set_index("stock_code")["ksic_mid_2digit"].get(stock)
    if b is None or n is None or code is None:
        return None
    mb = dp_industry_medians(table, buy, code)
    mn = dp_industry_medians(table, now, code)
    pm_own  = DUPONT_SELF_MEDIAN_OWN_CHANGE and _is_self_median(b, mb, "PM_pct")
    ato_own = DUPONT_SELF_MEDIAN_OWN_CHANGE and _is_self_median(b, mb, "ATO")
    dpm = dato = None
    if pm_own and pd.notna(b["PM_pct"]) and pd.notna(n["PM_pct"]):
        dpm = n["PM_pct"] - b["PM_pct"]
    elif pd.notna(b["PM_pct"]) and pd.notna(n["PM_pct"]) and pd.notna(mb["PM_median"]) and pd.notna(mn["PM_median"]):
        dpm = (n["PM_pct"] - mn["PM_median"]) - (b["PM_pct"] - mb["PM_median"])
    if ato_own and pd.notna(b["ATO"]) and pd.notna(n["ATO"]) and b["ATO"] != 0:
        dato = (n["ATO"] / b["ATO"] - 1) * 100
    elif pd.notna(b["ATO"]) and pd.notna(n["ATO"]) and b["ATO"] not in (None, 0) \
         and mb["ATO_median"] not in (None, 0) and mn["ATO_median"] not in (None, 0):
        rel_buy = _safe_ratio(b["ATO"], mb["ATO_median"])
        rel_now = _safe_ratio(n["ATO"], mn["ATO_median"])
        if rel_buy not in (None, 0) and rel_now is not None:
            dato = (rel_now / rel_buy - 1) * 100
    return score_dupont(dpm, dato)

def main():
    ksic  = load_ksic_from_base(RD)
    table = build_flat_table(RD, ksic)
    snaps = dp_list_snapshot_dates(RD)
    stocks = ksic["stock_code"].tolist()
    rec = snaps[5:]   # 2021-01 이후 = 실제 라이브 매수 범위
    pairs = [(rec[i], rec[-1]) for i in range(0, len(rec)-2, 2)]
    pairs += [(rec[0], rec[len(rec)//2]), (rec[1], rec[-2]), (rec[3], rec[-1])]
    same = diff = skip = 0; mism = []
    for s in stocks:
        for buy, now in pairs:
            snap_score = dupont_compare_pit(RD, ksic, s, buy, now)["score"]
            flat_score = dupont_flat_score(table, ksic, s, buy, now)
            if snap_score is None and flat_score is None:
                skip += 1
            elif snap_score == flat_score:
                same += 1
            else:
                diff += 1; mism.append((s, str(buy)[:10], str(now)[:10], snap_score, flat_score))
    print(f"\n일치 {same} | 불일치 {diff} | 둘다 없음 {skip}  (표본 {len(stocks)}종목 × {len(pairs)}쌍)")
    if diff:
        from collections import Counter
        print("불일치 매수일 분포:", dict(Counter(m[1] for m in mism)))
        for m in mism[:15]:
            print("  ", m)
    else:
        print("✓ 완전 일치 — 플랫 CSV 방식이 스냅샷 방식과 동일. 데이터팀 출력에 직접 연결 안전.")

if __name__ == "__main__":
    main()
