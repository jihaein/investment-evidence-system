import os
import pandas as pd
from dotenv import load_dotenv

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

ttm = load_ttm()
ttm.shape
ttm.dtypes

# loader for KSIC 
# file 12, TWO header rows → header=0, skiprows=[1]

def load_ksic() -> pd.DataFrame:
    # Load the environment variables from the .env file
    load_dotenv()

    # Get the Google Drive path
    data_dir = os.getenv("SEOSEOHI_DATA_DIR")
    file_ksic_path = os.path.join(data_dir, "12_기업개황_KSIC.csv")

    # Read the file data  
    data_ksic = pd.read_csv(file_ksic_path, header=0, skiprows=[1], dtype=str, encoding="utf-8-sig")

    return data_ksic

ksic = load_ksic()
ksic.shape

def ttm_asof(ttm, stock_code, asof_date) -> pd.Series | None:
    asof = pd.Timestamp(asof_date)
    ttm_filtered = ttm[(ttm['stock_code'] == stock_code) & (ttm['trade_date'] <= asof)]
    if ttm_filtered.empty:
        return None
    latest_ttm = ttm_filtered.loc[ttm_filtered['trade_date'].idxmax()]
    return latest_ttm

ttm = load_ttm()
ttm_asof(ttm, "036890", "2019-12-30")["trade_date"]   # → 2019-12-30
ttm_asof(ttm, "036890", "2019-12-29")["trade_date"]   # → 2019-12-27 (Sunday falls back to Friday)
ttm_asof(ttm, "036890", "2015-01-10")["sales_ttm"]    # → NaN (row exists, no TTM yet)

def ttm_asof_all(ttm, asof_date) -> pd.DataFrame:
    asof = pd.Timestamp(asof_date)
    ttm_filtered = ttm[ttm['trade_date'] <= asof]
    if ttm_filtered.empty:
        return pd.DataFrame()  # Return an empty DataFrame if no data is available
    latest_ttm = ttm_filtered.loc[ttm_filtered.groupby('stock_code')['trade_date'].idxmax()]
    return latest_ttm

snap = ttm_asof_all(ttm, "2019-12-30")
snap.shape
snap.stock_code.nunique()  == len(snap)

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

company_pm_at_date(ttm, "036890", "2019-12-30")["pm"]   # ≈ 8.4987
company_pm_at_date(ttm, "053660", "2017-06-30")["pm"]   # ≈ 5.1888
company_pm_at_date(ttm, "089010", "2019-12-30")["pm"]   # ≈ 6.6582

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

industry_pm_median_asof(ttm, ksic, "29", "2019-12-30")
# → PM_median ≈ 8.4987, n_peers = 3, fallback = False, basis "KSIC 29 (n=3)"
industry_pm_median_asof(ttm, ksic, "26", "2019-12-30")
# → PM_median ≈ 9.0921, n_peers = 4, fallback = False
industry_pm_median_asof(ttm, ksic, "25", "2019-12-30")
# → n_peers = 2 < 3 → fallback = True, PM_median ≈ 4.0262 (whole sample)

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

# real data
dupont_pm_compare(ttm, ksic, "053660", "2017-06-30", "2019-12-30")["dPM_adj_pp"]

# slide-6 sanity: temporarily hard-code buy pm=15/med=12, now pm=9/med=10 → expect -4