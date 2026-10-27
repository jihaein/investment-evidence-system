#fscore.py

from pathlib import Path
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data" / "seoseohi_main"
ANNUAL_FILE = DATA_DIR / "02_재무_연간_2013-2019.csv"
LTD_FILE = DATA_DIR / "30_총장기부채_합계확인상태.csv"
ISSUE_FILE = DATA_DIR / "09_증자감자현황_2015-2019.csv"
NEW_SHARE_TYPES = ["유상증자"]

#LTD = long-term debt, 장기부채
#총 장기부채 = 장기차입금 + 사채 + 유동성장기차입금 + 유동성사채
#ltd = 그해 말 총장기부채(당기)
#ltd_prior = 전년 말 총장기부채(전기)

# 2번 원본 컬럼명
ANNUAL_COLUMNS = {
    "ifrs_ProfitLoss": "ni", #당기순이익(일반) #f1, f3, f4
    "ifrs_CashFlowsFromUsedInOperatingActivities": "cfo", #영업활동현금흐름 #f2, f4
    "ifrs_Assets": "assets", #자산총계 #f1~f5, f9
    "ifrs_CurrentAssets": "current_assets", #유동자산 #f6
    "ifrs_CurrentLiabilities": "current_liabilities", #유동부채 #f6
    "ifrs_Revenue": "revenue", #매출액 #f8,9
    "ifrs_CostOfSales": "cost_of_sales", #매출원가 #f8(매출총이익이 비었을 때 대신 계산)
    "ifrs_GrossProfit": "gross_profit", #매출총이익 #f8
}

# step1

# read a csv as text
# skip the korean header line only when the file has one
def read_csv_text(path, has_korean_header):
    skip = [1] if has_korean_header else None
    return pd.read_csv(path, header=0, skiprows=skip, dtype=str, keep_default_na=False, encoding="utf-8-sig")

# annual financials: one row per company and fiscal year
def load_annual(path):
    df = read_csv_text(path, has_korean_header=True)
    out = df[["stock_code", "stock_name_2014", "fs_div", "fiscal_year", "rcept_no", "disclosed_date"]].copy()
    out = out.rename(columns={"stock_name_2014": "company_name"})
    out["fiscal_year"] = out["fiscal_year"].astype(int)
    out["disclosed_date"] = pd.to_datetime(out["disclosed_date"])
    for source, name in ANNUAL_COLUMNS.items():
        out[name] = pd.to_numeric(df[source], errors="coerce")
    return out

# total long-term debt: current and prior year-end
def load_ltd(path):
    df = read_csv_text(path, has_korean_header=False)
    out = df[["stock_code", "fs_div", "fiscal_year", "status"]].copy()
    out = out.rename(columns={"fs_div": "ltd_fs_div", "status": "ltd_status"})
    out["fiscal_year"] = out["fiscal_year"].astype(int)
    out["ltd"] = pd.to_numeric(df["total_long_term_debt"], errors="coerce")
    out["ltd_prior"] = pd.to_numeric(df["total_long_term_debt_prior"], errors="coerce")
    return out

# put annual financials and long-term debt side by side
def combine(annual, ltd):
    merged = annual.merge(ltd, on=["stock_code", "fiscal_year"], how="left")
    merged["fs_div_match"] = merged["ltd_fs_div"].isna() | (merged["fs_div"] == merged["ltd_fs_div"])
    return merged.sort_values(["stock_code", "fiscal_year"]).reset_index(drop=True)

# step1 result: 02 133행, 30 95행(2015-2019), 19종목, 2013-2019, 중복/기준 불일치0
# 30번은 2015년부터라 2013,2014년 ltd는 빈칸, f5는 같은 줄의 ltd, ltd_prior를 쓰므로 문제x


# step2. calcuate f1~f9 (except f7)

# attach values from 1 and 2 years before to each row
# 한 줄에 올해 값과 전년(전전년) 값이 나란히 오게 함.
# shift 대신 merge: 중간 연도가 빠져 있어도 엉뚱한 해의 값이 불지 않음
def add_prior_years(data):
    prev = data[["stock_code", "fiscal_year", "fs_div", "ni", "assets", "current_assets", "current_liabilities", "revenue", "gross_profit", "cost_of_sales"]].copy()
    prev["fiscal_year"] = prev["fiscal_year"] + 1
    prev = prev.add_suffix("_prev").rename(columns={
        "stock_code_prev": "stock_code", "fiscal_year_prev": "fiscal_year"})

    prev2 = data[["stock_code", "fiscal_year", "assets"]].copy()
    prev2["fiscal_year"] = prev2["fiscal_year"] + 2
    prev2 = prev2.rename(columns={"assets": "assets_prev2"})

    out = data.merge(prev, on=["stock_code", "fiscal_year"], how="left")
    out = out.merge(prev2, on=["stock_code", "fiscal_year"], how="left")

    # 올해와 작년의 재무제표 기준(연결/별도)이 다르면 같은 잣대 비교가 아님(표시만, 처리는 추후 결정)
    out["basis_changed"] = out["fs_div_prev"].notna() & (out["fs_div"] != out["fs_div_prev"])
    return out

# turn a condition into a 0/1 signal, but leave it empty when the value is missing
def to_signal(condition,value):
    signal = condition.astype("Int64")
    signal[value.isna()] = pd.NA
    return signal

# values shared by serveral signals: average total assets, gross profit
def add_base_values(df):
    out = df.copy()

    # 기초/기말 평균 총자산(f5, f9 분모)
    out["avg_assets"] = (out["assets"] + out["assets_prev"]) / 2
    out["avg_assets_prev"] = (out["assets_prev"] + out["assets_prev2"]) / 2

    # 매출총이익이 비어 있으면 매출 - 매출원가
    out["gp"] = out["gross_profit"].fillna(out["revenue"] - out["cost_of_sales"])
    out["gp_prev"] = out["gross_profit_prev"].fillna(out["revenue_prev"] - out["cost_of_sales_prev"])
    return out

#-------------------------------------

# F1. ROA > 0 -> 1점 (이익을 냈는가)
# ROA: return on assets
def add_f1_roa(df):
    out = df.copy()
    out["roa"] = out["ni"] / out["assets_prev"]
    out["f1_roa"] = to_signal(out["roa"] > 0, out["roa"])
    return out


# F2. 영업현금흐름/ 기초 총자산 > 0 -> 1점 (영업으로 현금이 들어왔는가)
def add_f2_cfo(df):
    out = df.copy()
    out["cfo_ratio"] = out["cfo"] / out["assets_prev"]
    out["f2_cfo"] = to_signal(out["cfo_ratio"] > 0, out["cfo_ratio"])
    return out


# F3. ROA가 전년보다 상승 -> 1점(수익성이 개선됐는가)
# F1에 의존하지 않도록 ROA를 여기서 다시 계산
def add_f3_delta_roa(df):
    out = df.copy()
    roa = out["ni"] / out["assets_prev"]
    out["roa_prev"] = out["ni_prev"] / out["assets_prev2"]
    out["delta_roa"] = roa - out["roa_prev"]
    out["f3_delta_roa"] = to_signal(out["delta_roa"] > 0, out["delta_roa"])
    return out


# F4.(순이익 - 영업현금흐름) / 기초 총자산 < 0 -> 1점 (이익이 현금으로 뒷받침되는가)
def add_f4_accrual(df):
    out = df.copy()
    out["accrual"] = (out["ni"] - out["cfo"]) / out["assets_prev"]
    out["f4_accrual"] = to_signal(out["accrual"] < 0, out["accrual"])
    return out


# F5. 장기부채/ 평균 총자산이 전년보다 하락 -> 1점 (장기 빚 부담이 줄었는가)
# 참고: 2년 연속 장기부채 0이면 변화가 0이라 0점 (원 논문 규칙)
def add_f5_delta_lever(df):
    out = df.copy()
    out["lever"] = out["ltd"] / out["avg_assets"]
    out["lever_prev"] = out["ltd_prior"] / out["avg_assets_prev"]
    out["delta_lever"] = out["lever"] - out["lever_prev"]
    out["f5_delta_lever"] = to_signal(out["delta_lever"] < 0, out["delta_lever"])
    return out


# F6. 유동비율이 전년보다 상승 -> 1점 (단기 지급 능력이 좋아졌는가)
def add_f6_delta_liquid(df):
    out = df.copy()
    out["liquid"] = out["current_assets"] / out["current_liabilities"]
    out["liquid_prev"] = out["current_assets_prev"] / out["current_liabilities_prev"]
    out["delta_liquid"] = out["liquid"] - out["liquid_prev"]
    out["f6_delta_liquid"] = to_signal(out["delta_liquid"] > 0, out["delta_liquid"])
    return out


# F7. 보통주 신규 발행 없음 -> 1점 (발행 형태 규칙 결정 필요)

# read share issuance records: one row per issuance
def load_issues(path):
    df = read_csv_text(path, has_korean_header=True)
    out = pd.DataFrame({
        "stock_code": df["stock_code"],
        "issue_date": pd.to_datetime(df["isu_dcrs_de"], format="%Y.%m.%d", errors="coerce"),
        "issue_type": df["isu_dcrs_stle"],
        "stock_kind": df["isu_dcrs_stock_knd"],
        "quantity": pd.to_numeric(df["isu_dcrs_qy"].str.replace(",", ""), errors="coerce"),
        "issue_rcept_no": df["rcept_no"],
    })
    out = out.dropna(subset=["issue_date"])
    out = out.drop_duplicates(subset=["stock_code", "issue_date", "issue_type", "stock_kind", "quantity"])
    out["fiscal_year"] = out["issue_date"].dt.year
    return out

# count new common share offers per company and year
def count_equity_offers(issues, new_share_types):
    is_common = issues["stock_kind"] == "보통주"
    is_offer = issues["issue_type"].str.contains("|".join(new_share_types))
    offers = issues[is_common & is_offer]
    counts = offers.groupby(["stock_code", "fiscal_year"]).size()
    return counts.rename("equity_offer_count").reset_index()

# put the offer count next to each company-year now
def attach_equity_offers(data, offer_counts):
    out = data.merge(offer_counts, on=["stock_code", "fiscal_year"], how="left")
    out["equity_offer_count"] = out["equity_offer_count"].fillna(0).astype(int)
    return out

# F7. 그해 보통주 신규 발행 없음 -> 1점 (외부 자금 조달에 기대지 않았는가)
def add_f7_no_equity_offer(df):
    out = df.copy()
    out["f7_no_equity_offer"] = (out["equity_offer_count"] == 0).astype("Int64")
    return out



# F8. 매출총이익률이 전년보다 상승 -> 1점 (매출 대비 남는 돈이 늘었는가)
# 영업이익률이 아니라 매출총이익률을 씀
def add_f8_delta_margin(df):
    out = df.copy()
    out["margin"] = out["gp"] / out["revenue"]
    out["margin_prev"] = out["gp_prev"] / out["revenue_prev"]
    out["delta_margin"] = out["margin"] - out["margin_prev"]
    out["f8_delta_margin"] = to_signal(out["delta_margin"] > 0, out["delta_margin"])
    return out


# F9. 매출 / 평균 총자산이 전년보다 상승 -> 1점 (자산으로 매출을 더 잘 만드는가)
def add_f9_delta_turn(df):
    out = df.copy()
    out["turn"] = out["revenue"] / out["avg_assets"]
    out["turn_prev"] = out["revenue_prev"] / out["avg_assets_prev"]
    out["delta_turn"] = out["turn"] - out["turn_prev"]
    out["f9_delta_turn"] = to_signal(out["delta_turn"] > 0, out["delta_turn"])
    return out


# 실행 순서
SIGNAL_STEPS = [
    add_base_values,
    add_f1_roa,
    add_f2_cfo,
    add_f3_delta_roa,
    add_f4_accrual,
    add_f5_delta_lever,
    add_f6_delta_liquid,
    add_f7_no_equity_offer,
    add_f8_delta_margin,
    add_f9_delta_turn,
]

SIGNAL_COLUMNS = [
    "f1_roa", "f2_cfo", "f3_delta_roa", "f4_accrual", "f5_delta_lever", "f6_delta_liquid", "f7_no_equity_offer", "f8_delta_margin", "f9_delta_turn"
]

# run every step in order
def add_signals(df):
    out = df
    for step in SIGNAL_STEPS:
        out = step(out)
    return out

# sum the nine signals into the F-Score (0~9)
# 신호 중 하나라도 빈칸이면 총점도 빈칸(일부만 더한 점수는 다른 회사와 비교할 수 없음)
def add_fscore(df):
    out = df.copy()
    out["f_score"] = out[SIGNAL_COLUMNS].sum(axis=1, min_count=len(SIGNAL_COLUMNS))
    out["f_score"] = out["f_score"].astype("Int64")
    out[SIGNAL_COLUMNS].sum(axis=1, min_count=len(SIGNAL_COLUMNS))
    return out


# 실행
if __name__ == "__main__":
    pd.set_option("display.float_format", "{:,.0f}".format)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", None)
 
    # ---- 1단계: 읽기 ----
    annual = load_annual(ANNUAL_FILE)
    ltd = load_ltd(LTD_FILE)
    data = combine(annual, ltd)

    # ---- 3단계: 신주발행 (add_signals보다 먼저!) ----
    issues = load_issues(ISSUE_FILE)
    offer_counts = count_equity_offers(issues, NEW_SHARE_TYPES)
    data = attach_equity_offers(data, offer_counts)

    # ---- 2단계: 신호 계산 ----
    data = add_prior_years(data)
    data = add_signals(data)
    data = data[data["fiscal_year"] >= 2015]
    data = add_fscore(data)

 
    print("계산 대상 행:", len(data), "(기대 95 = 19종목 × 5년)")
    print("기준 변경 행:", data["basis_changed"].sum())
    print()
    print("[신호별 빈칸 수]")
    print(data[SIGNAL_COLUMNS].isna().sum())

 
    # 손계산 대조: 대한광통신 2015년
    pd.set_option("display.float_format", "{:,.4f}".format)
    check = data[(data["stock_code"] == "010170") & (data["fiscal_year"] == 2015)]
    print()
    print(check[["roa", "roa_prev", "delta_roa", "accrual", "lever", "lever_prev",
                 "liquid", "liquid_prev", "margin", "margin_prev", "turn", "turn_prev"]].T)
    print(check[SIGNAL_COLUMNS].T)

    print("계산 대상 행:", len(data))
    print("F-Score 빈칸:", data["f_score"].isna().sum())
    print()
    print("[F-Score 분포]")
    print(data["f_score"].value_counts().sort_index())
    print()
    print("[회사 × 연도 F-Score]")
    print(data.pivot(index="company_name", columns="fiscal_year", values="f_score"))