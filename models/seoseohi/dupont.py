import os
from dotenv import load_dotenv
import pandas as pd
import sys
sys.path.insert(0, "/Users/jihaein/Desktop/fds_main/investment-evidence-system")
from models.seoseohi.config import DUPONT_PM_ADJ_CUTS, DUPONT_ATO_REL_CUTS


def load_ttm() -> pd.DataFrame: 
    # Load the environment variables from the .env file
    load_dotenv()

    # Get the Google Drive path
    ttm_path = os.getenv("SEOSEOHI_TTM_DIR")
    file_ttm_path = os.path.join(ttm_path, "ttm_final.csv")

    # Read the file data  
    data_ttm = pd.read_csv(file_ttm_path, header=0, dtype=str, encoding="utf-8-sig")

    # type casting TTM data
    data_ttm[['sales_ttm', 'operating_profit_ttm', 'ni_ttm', 'ni_owner_ttm', 'cfo_ttm']] = data_ttm[['sales_ttm', 'operating_profit_ttm', 'ni_ttm', 'ni_owner_ttm', 'cfo_ttm']].apply(pd.to_numeric, errors='coerce')
    data_ttm[['ttm_as_of_available_from', 'trade_date']] = data_ttm[['ttm_as_of_available_from', 'trade_date']].apply(pd.to_datetime, errors='coerce')

    return data_ttm

# loader for KSIC 
# file 12, TWO header rows → header=0, skiprows=[1]

def load_ksic() -> pd.DataFrame:
    # Load the environment variables from the .env file
    load_dotenv()

    # Get the Google Drive path
    file_ksic_path = os.path.join(os.getenv("SEOSEOHI_FINAL_DATA_DIR"), "12_기업개황_KSIC.csv")

    # Read the file data  
    data_ksic = pd.read_csv(file_ksic_path, header=0, skiprows=[1], dtype=str, encoding="utf-8-sig")

    return data_ksic

def filter_to_universe(ttm, ksic):
    universe = set(ksic["stock_code"].str.zfill(6))
    ttm = ttm[ttm["stock_code"].str.zfill(6).isin(universe)]
    return ttm


def load_stocks() -> pd.DataFrame:
    load_dotenv()
    path = os.path.join(os.getenv("SEOSEOHI_FINAL_DATA_DIR"), "01_종목목록.csv")
    df = pd.read_csv(path, header=0, skiprows=[1], dtype=str, encoding="utf-8-sig")
    df["stock_code"] = df["stock_code"].str.zfill(6)
    return df

def delisting_flag(stocks, stock_code) -> dict:
    row = stocks[stocks.stock_code == stock_code]
    if row.empty:
        return {"delisted": None, "delisted_in_window": None}
    r = row.iloc[0]
    return {"delisted": r["delisting_status"] == "상장폐지 확인",
            "delisted_in_window": r["delisted_2015_2019"] == "예"}



# returns the ttm row (series) available as of 'asof_date'
# the latest trading day on or before it. None if nothing is available yet.
def ttm_asof(ttm, stock_code, asof_date) -> pd.Series | None:
    asof = pd.Timestamp(asof_date)
    ttm_filtered = ttm[(ttm['stock_code'] == stock_code) & (ttm['trade_date'] <= asof)]
    if ttm_filtered.empty:
        return None
    latest_ttm = ttm_filtered.loc[ttm_filtered['trade_date'].idxmax()]
    return latest_ttm


# returns the industry ttm as of the date of buy
def ttm_asof_all(ttm, asof_date) -> pd.DataFrame:
    asof = pd.Timestamp(asof_date)
    ttm_filtered = ttm[ttm['trade_date'] <= asof]
    if ttm_filtered.empty:
        return pd.DataFrame()  # Return an empty DataFrame if no data is available
    latest_ttm = ttm_filtered.loc[ttm_filtered.groupby('stock_code')['trade_date'].idxmax()]
    return latest_ttm


# Step D: company PM at a date 

def company_pm_at_date(ttm, stock_code, asof_date) -> dict:
    row = ttm_asof(ttm, stock_code, asof_date)
    if row is None:
        return {"stock_code": stock_code, "asof_date": asof_date, "pm": None, "note": "기준일 이전 자료가 없습니다."}

    sales = row['sales_ttm']
    oi = row['operating_profit_ttm']
    fs_div = row['fs_div']
    trade_date = row['trade_date']
    ttm_trigger_receipt = row['ttm_trigger_receipt']

    if pd.isna(sales) or pd.isna(oi) or sales == 0:
        return {"stock_code": stock_code, "asof_date": asof_date, "pm": None, "note": "매출액 또는 영업이익이 결측치입니다."}

    pm = oi / sales * 100

    return {"stock_code": stock_code, "asof_date": asof_date, "pm": pm, "fs_div": fs_div, "trade_date": trade_date, "ttm_trigger_receipt": ttm_trigger_receipt, "note": None}


# Step #E : Industry median at a date

# attach KSIC to every company
def with_ksic(ttm, ksic) -> pd.DataFrame:
    ttm_with_ksic = ttm.merge(ksic[['stock_code', 'ksic_mid_2digit']], on='stock_code', how='left')
    return ttm_with_ksic

# the median
def industry_pm_median_asof(ttm, ksic,ksic_code, asof_date, min_peers=3) -> dict:
    snap = ttm_asof_all(ttm, asof_date)

    snap = snap.copy()
    snap["PM"] = snap["operating_profit_ttm"] / snap["sales_ttm"] * 100
    snap = snap[snap["PM"].notna()]

    snap_with_ksic = with_ksic(snap, ksic)
    peers = snap_with_ksic[snap_with_ksic.ksic_mid_2digit == ksic_code]

    if len(peers) >= min_peers:
        median = peers["PM"].median()
        basis = f"KSIC {ksic_code} (n={len(peers)})"
    else:
        median = snap_with_ksic["PM"].median() # fallback
        basis = f"전체 표본 대체"

    fallback = len(peers) < min_peers

    return {"PM_median": median, "n_peers": len(peers), "basis": basis, "fallback": fallback}


# Step F: Adjusted PM and the change

def dupont_pm_compare(ttm, ksic, stock_code, buy_date, now_date) -> dict:
    # 1) get the company's PM and its KSIC code at both dates (step D + the KSIC map)
    buy = company_pm_at_date(ttm, stock_code, buy_date)
    now = company_pm_at_date(ttm, stock_code, now_date)

    code = ksic.set_index("stock_code")["ksic_mid_2digit"].get(stock_code)

    if buy["pm"] is None or now["pm"] is None or code is None:
        return {"stock_code": stock_code, "dPM_adj_pp": None, "note": "계산에 필요한 값이 결측입니다."}

    # 2) Get the industry median PM at both dates (Step E) using that company's KSIC code
    med_buy  = industry_pm_median_asof(ttm, ksic, code, buy_date)
    med_now = industry_pm_median_asof(ttm, ksic, code, now_date)

    # 4) dPM_adj_pp = PM_adj_now - PM_adj_buy (units: %p)
    PM_adj_buy = buy["pm"] - med_buy["PM_median"]
    PM_adj_now = now["pm"] - med_now["PM_median"]
    dPM_adj_pp = PM_adj_now - PM_adj_buy

    fs_div_flag = buy["fs_div"] != now["fs_div"]

    return {
    "stock_code": stock_code,
    "buy_date": buy_date,
    "now_date": now_date,
    "PM_adj_buy": PM_adj_buy,
    "PM_adj_now": PM_adj_now,
    "dPM_adj_pp": dPM_adj_pp,
    "fs_div_flag": fs_div_flag,
    "basis_buy": med_buy["basis"],
    "basis_now": med_now["basis"],
    "note": None,
    }

# slide-6 sanity: temporarily hard-code buy pm=15/med=12, now pm=9/med=10 → expect -4

def load_assets(path) -> pd.DataFrame:
    df = pd.read_csv(path, header=0, skiprows=[1], dtype=str, encoding="utf-8-sig")
    df["stock_code"] = df["stock_code"].str.zfill(6)
    df[['ifrs_Assets_thstrm_amount', 'bsns_year']] = df[['ifrs_Assets_thstrm_amount', 'bsns_year']].apply(pd.to_numeric, errors='coerce')
    df[['disclosed_date']] = df[['disclosed_date']].apply(pd.to_datetime, errors='coerce')

    df = df.sort_values("disclosed_date").drop_duplicates(
      ["stock_code", "fs_div", "bsns_year", "reprt_code"], keep="last")

    return df


def assets_asof(assets, stock_code, fs_div, ttm_trigger_receipt) -> dict: 
    
    # Lookup 1: A_t via rcept_no
    m = assets[(assets.stock_code == stock_code) & 
               (assets.fs_div == fs_div) &
               (assets.rcept_no == ttm_trigger_receipt)] 

    if m.empty: 
        return {"A_t": None, "A_t_4q": None, "note": "자산 매칭 안됨(해당 분기 자산 없음)"}
    row = m.iloc[0]
    A_t = row["ifrs_Assets_thstrm_amount"]; y = row["bsns_year"]; rc = row["reprt_code"]

    # Lookup 2: A_t_4q via (year-1, same reprt_code)
    m4 = assets[(assets.stock_code==stock_code) & (assets.fs_div==fs_div) &
                (assets.bsns_year==y-1) & (assets.reprt_code==rc)]

    if m4.empty: 
        return {"A_t": A_t, "A_t_4q": None, "note": f"{y-1} {rc} 자산 없음"}

    A_t_4q = m4.iloc[0]["ifrs_Assets_thstrm_amount"]

    return {"A_t": A_t, "A_t_4q": A_t_4q, "period": f"{y}-{rc}", "note": None}


def score_pm(dPM_adj_pp) -> int | None: 
    """
    PM 점수 계산
    :param dPM_adj_pp: PM_adj_pp 값
    :return: PM 점수
    """
    if dPM_adj_pp is None:
        return None
    count = sum(1 for c in DUPONT_PM_ADJ_CUTS if dPM_adj_pp <= c)
    return count


# this function mirros company_pm_at_date, but divides sales by average assets instead 

def company_ato_at_date(ttm, assets, stock_code, asof_date) -> dict:
    row = ttm_asof(ttm, stock_code, asof_date)
    if row is None: 
        return {"stock_code": stock_code, "asof_date": asof_date, "ato": None, "note": "기준일 이전 자료 없음"}

    sales = row["sales_ttm"]
    if pd.isna(sales):
        return {"stock_code": stock_code, "asof_date": asof_date, "ato": None, "note": "기준일 이전 자료 없음"}

    a = assets_asof(assets, stock_code, row["fs_div"], row["ttm_trigger_receipt"])
    if a["A_t"] is None or a["A_t_4q"] is None: 
        return {"stock_code": stock_code, "asof_date": asof_date, "ato": None, "note": a["note"]}

    avg = (a["A_t"] + a["A_t_4q"]) / 2
    if avg == 0:
        return {"stock_code": stock_code, "asof_date": asof_date, "ato": None, "note": a["note"]}

    ato = sales / avg
    return {"stock_code": stock_code, "asof_date": asof_date, "ato": ato, "fs_div": row["fs_div"], "trade_date": row["trade_date"], "period": a["period"], "note": None}



# step 4) this function mirrors industry_pm_median_asof.

def industry_ato_median_asof(ttm, assets, ksic, ksic_code, asof_date, min_peers=3) -> dict:
    snap = ttm_asof_all(ttm, asof_date).copy()
    # build the ATO column (one call per company, reusing your tested function)
    snap["ATO"] = [company_ato_at_date(ttm, assets, s, asof_date)["ato"] for s in snap["stock_code"]]
    snap = snap[snap["ATO"].notna()]

    snap_with_ksic = with_ksic(snap, ksic)
    peers = snap_with_ksic[snap_with_ksic.ksic_mid_2digit == ksic_code]

    if len(peers) >= min_peers:
        median = peers["ATO"].median()
        basis = f"KSIC {ksic_code} (n={len(peers)})"
    else:
        median = snap_with_ksic["ATO"].median()
        basis = "전체 표본 대체"

    fallback = len(peers) < min_peers
    return {"ATO_median": median, "n_peers": len(peers), "basis": basis, "fallback": fallback}


def dupont_ato_compare(ttm, assets, ksic, stock_code, buy_date, now_date) -> dict:
    buy = company_ato_at_date(ttm, assets, stock_code, buy_date)
    now = company_ato_at_date(ttm, assets, stock_code, now_date)
    code = ksic.set_index("stock_code")["ksic_mid_2digit"].get(stock_code)

    if buy["ato"] is None or now["ato"] is None or code is None:
        return {"stock_code": stock_code, "dATO_rel_pct": None, "note": "계산에 필요한 값이 결측입니다."}

    med_buy = industry_ato_median_asof(ttm, assets, ksic, code, buy_date)
    med_now = industry_ato_median_asof(ttm, assets, ksic, code, now_date)

    mb = med_buy["ATO_median"]; mn = med_now["ATO_median"]
    if mb is None or mn is None or mb == 0 or mn == 0:
        return {"stock_code": stock_code, "dATO_rel_pct": None, "note": "업종 ATO 중위수가 0 또는 결측"}

    ATO_rel_buy = buy["ato"] / mb
    ATO_rel_now = now["ato"] / mn
    if ATO_rel_buy == 0: 
        return {"stock_code": stock_code, "dATO_rel_pct": None, "note": "매수 시점 ATO_rel이 0"}
    dATO_rel_pct = (ATO_rel_now / ATO_rel_buy - 1) * 100

    return {"stock_code": stock_code, "buy_date": buy_date, "now_date": now_date,
            "ATO_rel_buy": ATO_rel_buy, "ATO_rel_now": ATO_rel_now,
            "dATO_rel_pct": dATO_rel_pct, "note": None}

def _dupont_level(value, cuts):
    return sum(1 for c in cuts if value <= c)

def score_dupont(dPM_adj_pp, dATO_rel_pct) -> int | None:
    lp = _dupont_level(dPM_adj_pp, DUPONT_PM_ADJ_CUTS) if dPM_adj_pp is not None else None
    la = _dupont_level(dATO_rel_pct, DUPONT_ATO_REL_CUTS) if dATO_rel_pct is not None else None
    if lp is None and la is None:
        return None
    if lp is None:            # PM 없으면 ATO만
        return la
    if la is None:            # ATO 없으면 PM만 (현진소재 같은 fs_div 전환 케이스)
        return lp
    score = max(lp, la)
    if lp >= 1 and la >= 1:   # 동반 악화면 한 단계 올림
        score = min(3, score + 1)
    return score


def dupont_compare(ttm, assets, ksic, stocks, stock_code, buy_date, now_date) -> dict:
    pm  = dupont_pm_compare(ttm, ksic, stock_code, buy_date, now_date)
    ato = dupont_ato_compare(ttm, assets, ksic, stock_code, buy_date, now_date)
    score = score_dupont(pm.get("dPM_adj_pp"), ato.get("dATO_rel_pct"))
    flag = delisting_flag(stocks, stock_code)
    return {"stock_code": stock_code, "buy_date": buy_date, "now_date": now_date,
            "dPM_adj_pp": pm.get("dPM_adj_pp"),
            "dATO_rel_pct": ato.get("dATO_rel_pct"),
            "score": score,
            "pm_detail": pm, "ato_detail": ato,
            "delisted": flag["delisted"],
            "delisted_in_window": flag["delisted_in_window"]}


# ============================================================
#  스냅샷(점별) 모드 — DuPont, 미래정보 차단(look-ahead bias) 방지
# ------------------------------------------------------------
#  [왜] 기존 방식은 거래일마다 한 줄인 큰 ttm_final.csv 에서 trade_date 로 '그날 이전'을
#  골랐다. 그 파일에 몇 달 뒤 반영된 정정 TTM 이 섞이면 과거 평가에 미래 숫자가 샐 수 있다.
#
#  [해법] 데이터팀이 기준일(21개)마다 '그날까지 공시된 분기 재무만' 담은 DuPont 스냅샷을
#  따로 만들어 줬다. 스냅샷 11 파일에는 PM(영업이익률)·ATO(총자산회전율)와 그 원재료
#  (TTM 매출·영업이익·당분기말/전년동기말 자산)가 이미 계산돼 있고, 11B 파일이 각 분기의
#  공시일(disclosed_date)을 준다. 평가일마다 기준일 ≤ 평가일 인 최신 스냅샷을 골라 그
#  폴더만으로 PM·ATO 변화를 구하므로, 자산 파일(03)이 따로 필요 없고 미래 유입도 없다.
#
#  스냅샷 폴더 구조:
#    <BACKTEST_DATA_DIR>/추가자료/기준일별_원본/DuPont/snapshots/<기준일>/
#        11_재무_DuPont_TTM.csv      (stock_code, fs_div, bsns_year, quarter, rcept_no,
#                                     ifrs_Revenue_ttm, dart_OperatingIncomeLoss_ttm,
#                                     ifrs_Assets_t, ifrs_Assets_t_4q, PM_pct, ATO)
#        11B_DuPont_날짜연결.csv     (같은 키 + disclosed_date, available_from, period_end)
#    업종분류(KSIC)는 기준일과 무관하므로 v3 루트의 12_기업개황_KSIC.csv 를 한 번만 읽는다.
# ============================================================

from pathlib import Path

DUPONT_SNAPSHOT_REL = Path("추가자료") / "기준일별_원본" / "DuPont" / "snapshots"
DP_NUMERIC = ["ifrs_Revenue_ttm", "dart_OperatingIncomeLoss_ttm",
              "ifrs_Assets_t", "ifrs_Assets_t_4q", "PM_pct", "ATO"]
DP_MERGE_KEYS = ["stock_code", "fs_div", "bsns_year", "quarter", "rcept_no"]


def _dp_snapshot_root(base_dir):
    return Path(base_dir) / DUPONT_SNAPSHOT_REL


def dp_list_snapshot_dates(base_dir):
    """기준일 폴더 이름들(오름차순)."""
    return sorted(p.name for p in _dp_snapshot_root(base_dir).iterdir() if p.is_dir())


def dp_resolve_snapshot_dir(base_dir, as_of_date):
    """as_of_date 이하의 기준일 중 가장 최근 DuPont 스냅샷 폴더. 없으면 None."""
    as_of = pd.Timestamp(as_of_date)
    chosen = None
    for name in dp_list_snapshot_dates(base_dir):
        if pd.Timestamp(name) <= as_of:
            chosen = name
        else:
            break
    return (_dp_snapshot_root(base_dir) / chosen) if chosen else None


def load_ksic_from_base(base_dir):
    """v3 루트의 12_기업개황_KSIC.csv → (stock_code, ksic_mid_2digit). 2줄 헤더."""
    path = Path(base_dir) / "12_기업개황_KSIC.csv"
    df = pd.read_csv(path, header=0, skiprows=[1], dtype=str, encoding="utf-8-sig")
    df["stock_code"] = df["stock_code"].str.zfill(6)
    return df[["stock_code", "ksic_mid_2digit"]]


# 같은 스냅샷을 여러 종목·여러 쌍이 다시 쓰므로 한 번 만든 표는 캐시한다 (기준일 21개뿐)
_DP_TABLE_CACHE = {}


def build_dupont_table_from_snapshot(snapshot_dir, ksic):
    """스냅샷 폴더 하나로 (종목×분기) PM·ATO 표를 만든다. 11 + 11B(공시일) + KSIC 를 붙인다."""
    key = str(snapshot_dir)
    if key in _DP_TABLE_CACHE:
        return _DP_TABLE_CACHE[key]

    d = Path(snapshot_dir)
    ttm = pd.read_csv(d / "11_재무_DuPont_TTM.csv", header=0, skiprows=[1], dtype=str, encoding="utf-8-sig")
    dates = pd.read_csv(d / "11B_DuPont_날짜연결.csv", header=0, skiprows=[1], dtype=str, encoding="utf-8-sig")
    ttm["stock_code"] = ttm["stock_code"].str.zfill(6)
    dates["stock_code"] = dates["stock_code"].str.zfill(6)
    for c in DP_NUMERIC:
        ttm[c] = pd.to_numeric(ttm[c], errors="coerce")

    m = ttm.merge(dates[DP_MERGE_KEYS + ["disclosed_date", "available_from", "period_end"]],
                  on=DP_MERGE_KEYS, how="left")
    m["disclosed_date"] = pd.to_datetime(m["disclosed_date"], errors="coerce")
    m = m.merge(ksic, on="stock_code", how="left")

    _DP_TABLE_CACHE[key] = m
    return m


def clear_dupont_snapshot_cache():
    _DP_TABLE_CACHE.clear()


# 스냅샷 안에서 그날(asof_date)까지 공시된 최신 분기 한 줄. 없으면 None
# (스냅샷 기준일 ≤ asof_date 이므로 보통 모든 행이 통과하지만, 안전하게 한 번 더 거른다)
def dp_latest_asof(table, stock_code, asof_date):
    asof = pd.Timestamp(asof_date)
    rows = table[(table["stock_code"] == stock_code) & (table["disclosed_date"] <= asof)]
    if rows.empty:
        return None
    return rows.loc[rows["disclosed_date"].idxmax()]


# 스냅샷 안에서 '그날 기준 종목별 최신 분기'만 모아 업종(KSIC) 중위수를 낸다
# 동종 peer 가 min_peers 미만이면 전체 표본으로 대체 (기존 industry_*_median_asof 규칙과 동일)
def dp_industry_medians(table, asof_date, ksic_code, min_peers=3):
    asof = pd.Timestamp(asof_date)
    snap = table[table["disclosed_date"] <= asof]
    if snap.empty:
        return {"PM_median": None, "ATO_median": None,
                "pm_fallback": True, "ato_fallback": True, "n_pm": 0, "n_ato": 0}
    latest = snap.loc[snap.groupby("stock_code")["disclosed_date"].idxmax()]

    def _median(col):
        peers = latest[(latest["ksic_mid_2digit"] == ksic_code) & latest[col].notna()]
        if len(peers) >= min_peers:
            return peers[col].median(), len(peers), False
        allv = latest[latest[col].notna()]
        return allv[col].median(), len(peers), True

    pm_med, n_pm, pm_fb = _median("PM_pct")
    ato_med, n_ato, ato_fb = _median("ATO")
    return {"PM_median": pm_med, "ATO_median": ato_med,
            "pm_fallback": pm_fb, "ato_fallback": ato_fb, "n_pm": n_pm, "n_ato": n_ato}


# 스냅샷 기반 매수일·평가일 DuPont 비교 (미래정보 차단판)
#   - PM 는 업종 중위수를 뺀 '조정 PM'의 변화(%p)
#   - ATO 는 업종 중위수로 나눈 '상대 ATO'의 변화(%)
#   점수는 기존 score_dupont(두 변화량)을 그대로 쓴다
def dupont_compare_pit(base_dir, ksic, stock_code, buy_date, now_date):
    out = {"stock_code": stock_code, "buy_date": buy_date, "now_date": now_date,
           "dPM_adj_pp": None, "dATO_rel_pct": None, "score": None, "note": None}

    buy_dir = dp_resolve_snapshot_dir(base_dir, buy_date)
    eval_dir = dp_resolve_snapshot_dir(base_dir, now_date)
    if buy_dir is None or eval_dir is None:
        out["note"] = "해당 날짜 이전 스냅샷이 없습니다."
        return out

    bt = build_dupont_table_from_snapshot(buy_dir, ksic)
    et = build_dupont_table_from_snapshot(eval_dir, ksic)
    b = dp_latest_asof(bt, stock_code, buy_date)
    n = dp_latest_asof(et, stock_code, now_date)
    code = ksic.set_index("stock_code")["ksic_mid_2digit"].get(stock_code)
    if b is None or n is None or code is None:
        out["note"] = "계산에 필요한 값이 결측입니다."
        return out

    mb = dp_industry_medians(bt, buy_date, code)
    mn = dp_industry_medians(et, now_date, code)

    # PM: 조정 PM = 회사 PM - 업종 중위수 PM, 그 변화(%p)
    if pd.notna(b["PM_pct"]) and pd.notna(n["PM_pct"]) and \
       pd.notna(mb["PM_median"]) and pd.notna(mn["PM_median"]):
        PM_adj_buy = b["PM_pct"] - mb["PM_median"]
        PM_adj_now = n["PM_pct"] - mn["PM_median"]
        out["dPM_adj_pp"] = PM_adj_now - PM_adj_buy

    # ATO: 상대 ATO = 회사 ATO / 업종 중위수 ATO, 그 변화(%)
    if pd.notna(b["ATO"]) and pd.notna(n["ATO"]) and \
       mb["ATO_median"] not in (None, 0) and mn["ATO_median"] not in (None, 0):
        rel_buy = b["ATO"] / mb["ATO_median"]
        rel_now = n["ATO"] / mn["ATO_median"]
        if rel_buy != 0:
            out["dATO_rel_pct"] = (rel_now / rel_buy - 1) * 100

    out["score"] = score_dupont(out["dPM_adj_pp"], out["dATO_rel_pct"])
    out["snapshot_buy"] = Path(buy_dir).name
    out["snapshot_eval"] = Path(eval_dir).name
    out["fs_div_flag"] = b["fs_div"] != n["fs_div"]
    out["pm_basis_buy"] = f"n={mb['n_pm']}" + ("(전체대체)" if mb["pm_fallback"] else "")
    out["pm_basis_now"] = f"n={mn['n_pm']}" + ("(전체대체)" if mn["pm_fallback"] else "")
    return out


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    ttm = load_ttm()
    ksic = load_ksic()
    ttm19 = filter_to_universe(ttm, ksic)
    print(ttm19["stock_code"].nunique())        # 19
    print("064260" in set(ttm19["stock_code"]))  # False

    # delister checkpoint
    stocks = load_stocks()
    print(delisting_flag(stocks, "053660"))  # 현진소재 → delisted True
    print(delisting_flag(stocks, "052270"))  # 우전 → delisted True, delisted_in_window True
    print(delisting_flag(stocks, "036890"))  # 진성 → delisted False
