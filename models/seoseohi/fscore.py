"""
fscore.py — 서서히 모델 중 Piotroski F-Score
[what]
회사 × 연도마다 재무제표로 9개 질문(F1~F9)에 0 또는 1점을 주고 합쳐 F-Score(0~9)를 만든다.
그다음 매수일과 평가일(현재 날짜)의 F-Score를 비교해 하락폭(매수일 점수 - 평가일 점수)을 구하고,
위험점수(0~2, 임시안)로 환산한다. 하락폭이 양수면 점수가 떨어진 것(위험 증가)이다.

[data] 
    02_재무_연간_2013-2019.csv              연간 재무
    30_총장기부채_합계확인상태.csv            총장기부채 당기·전기
    FScore_동일기준_비교기간보완.csv          기준(연결/별도)이 바뀐 해의 같은 기준 과거값 
    F7_보통주유상증자_연도별판정.csv          F7 최종 판정 
    09_증자감자현황_2015-2019.csv             F7 대조용

[rule]
    - 순이익은 기업 전체 순이익(ifrs_ProfitLoss). 지배주주 순이익이 아님
    - F5 장기부채는 30번 합계. 장기차입금 한 계정만 쓰지 않음
    - 연결·별도 금액을 섞어 전년 대비 개선을 판정하지 않음
    - F7은 보통주 유상증자만. 전환권·신주인수권·주식매수선택권 행사, 무상증자, 우선주, 철회 제외
    - F5: 2년 연속 장기부채가 0이면 1점 (무차입 유지는 양호한 상태. 정민 결정)
    - 위험점수: 하락폭으로 0~2점, 평가일 F-Score가 0~1점이면 +1점 → 최종 0~3점 (정민 결정)
    - 매수일·평가일이 같은 사업보고서면 위험점수는 유보 (정민 결정)
    - 빈칸은 0이 아니라 "값 없음"
    - 상장폐지 이후 기간은 분석 대상 아님

[progress]
    읽기 → F7 붙이기 → 전년·전전년 값 붙이기 → 같은 기준 과거값으로 교체
    → F1~F9 → F-Score → 확인 → 매수일·평가일 비교와 위험점수 → 저장
"""

from pathlib import Path
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data" / "fscore"         
OUTPUT_DIR = Path(__file__).parent / "output"

ANNUAL_FILE = DATA_DIR / "02_재무_연간_2013-2019.csv"
LTD_FILE = DATA_DIR / "30_총장기부채_합계확인상태.csv"
SAME_BASIS_FILE = DATA_DIR / "FScore_동일기준_비교기간보완.csv"
F7_FILE = DATA_DIR / "F7_보통주유상증자_연도별판정.csv"
ISSUE_FILE = DATA_DIR / "09_증자감자현황_2015-2019.csv"

FIRST_SCORE_YEAR = 2015                    # F-Score 산출 대상 첫 연도
DELISTED = {"052270": "2017-04-12"}        # 상장폐지 종목과 폐지일 (우전앤한단)

# ---- F-Score 변화 → 위험점수 (0~3점) ----
# 1차: 하락폭(매수일 점수 - 평가일 점수, 양수 = 점수가 떨어짐)으로 0~2점
#      위에서부터 검사해 처음 맞는 줄의 점수를 쓴다: (하락폭이 이 값 이상이면, 위험점수)
#      근거: 본 데이터 181개 조합에서 1년 보유 표준편차 2.49, 가운데 50%가 -2~+2.
#      19종목 x 5년으로 정한 초기 가설값이라 백테스팅 데이터로 다시 확인한다
RISK_SCALE_CONFIRMED = True    # 1차 기준은 정민 결정 ④의 전제로 사용됨. 정균 확인 전이면 False로
RISK_SCALE = [
    (5, 2),      # 5점 이상 하락 (매우 큰 하락)
    (3, 1),      # 3~4점 하락 (평소 범위를 벗어난 하락)
    (-9, 0),     # 그 외 (2점 이하 하락, 상승 포함)
]
# 2차: 평가일 F-Score가 이 값 이하이면 위험점수 +1 (재무적으로 약한 집단. Piotroski 2000)
LEVEL_ADD_MAX_SCORE = 1
LEVEL_ADD_POINTS = 1
HOLD_IF_SAME_REPORT = True     # 매수일·평가일이 같은 보고서면 위험점수 유보

ANNUAL_COLUMNS = {
    "ifrs_ProfitLoss": "ni",                                   # 당기순이익(기업 전체)   F1 F3 F4
    "ifrs_CashFlowsFromUsedInOperatingActivities": "cfo",      # 영업활동현금흐름        F2 F4
    "ifrs_Assets": "assets",                                   # 자산총계               F1~F5 F9
    "ifrs_CurrentAssets": "current_assets",                    # 유동자산               F6
    "ifrs_CurrentLiabilities": "current_liabilities",          # 유동부채               F6
    "ifrs_Revenue": "revenue",                                 # 매출액                 F8 F9
    "ifrs_CostOfSales": "cost_of_sales",                       # 매출원가 (매출총이익이 빈칸일 때만)
    "ifrs_GrossProfit": "gross_profit",                        # 매출총이익             F8
}

SAME_BASIS_COLUMNS = {
    "ifrs_Assets": "assets",
    "ifrs_ProfitLoss": "ni",
    "ifrs_Revenue": "revenue",
    "ifrs_GrossProfit": "gross_profit",
    "ifrs_CurrentAssets": "current_assets",
    "ifrs_CurrentLiabilities": "current_liabilities",
}

SIGNAL_COLUMNS = [
    "f1_roa", "f2_cfo", "f3_delta_roa", "f4_accrual",
    "f5_delta_lever", "f6_delta_liquid", "f7_no_equity_offer",
    "f8_delta_margin", "f9_delta_turn",
]


# 1. read data
def read_csv_text(path, has_korean_header):
    skip = [1] if has_korean_header else None
    return pd.read_csv(path, header=0, skiprows=skip, dtype=str,
                       keep_default_na=False, encoding="utf-8-sig")


# 02번: 회사 × 회계연도 한 줄
def load_annual(path):
    df = read_csv_text(path, has_korean_header=True)
    out = df[["stock_code", "stock_name_2014", "fs_div", "fiscal_year", "rcept_no", "disclosed_date"]].copy()
    out = out.rename(columns={"stock_name_2014": "company_name"})
    out["fiscal_year"] = out["fiscal_year"].astype(int)
    out["disclosed_date"] = pd.to_datetime(out["disclosed_date"])
    for source, name in ANNUAL_COLUMNS.items():
        out[name] = pd.to_numeric(df[source], errors="coerce")
    return out


# 30번: 총장기부채 당기말(ltd)과 전기말(ltd_prior)
def load_ltd(path):
    df = read_csv_text(path, has_korean_header=False)
    out = df[["stock_code", "fs_div", "fiscal_year"]].copy()
    out = out.rename(columns={"fs_div": "ltd_fs_div"})      # 02번 fs_div와 이름이 겹치지 않게
    out["fiscal_year"] = out["fiscal_year"].astype(int)
    out["ltd"] = pd.to_numeric(df["total_long_term_debt"], errors="coerce")
    out["ltd_prior"] = pd.to_numeric(df["total_long_term_debt_prior"], errors="coerce")
    return out


# 02번과 30번을 회사·연도로 옆에 붙인다
def combine(annual, ltd):
    merged = annual.merge(ltd, on=["stock_code", "fiscal_year"], how="left")
    # 두 파일의 재무제표 기준이 같은지 (30번에 값이 없으면 비교할 것이 없으므로 True)
    merged["ltd_fs_div_match"] = merged["ltd_fs_div"].isna() | (merged["fs_div"] == merged["ltd_fs_div"])
    return merged.sort_values(["stock_code", "fiscal_year"]).reset_index(drop=True)



# 2. F7 판정 붙이기
# F7 연도별판정: common_equity_issued=True이면 F7=0, False이면 F7=1
def load_f7(path):
    df = read_csv_text(path, has_korean_header=False)
    out = df[["stock_code", "fiscal_year", "status"]].copy()
    out = out.rename(columns={"status": "f7_status"})
    out["fiscal_year"] = out["fiscal_year"].astype(int)
    out["f7_score_official"] = pd.to_numeric(df["F7_score"], errors="coerce")
    return out


def attach_f7(data, f7):
    return data.merge(f7, on=["stock_code", "fiscal_year"], how="left")


# 3. 전년·전전년 값 붙이기와 같은 기준 과거값 교체

# 연도를 +1(+2) 한 복사본을 붙여, 한 줄에 올해 값과 전년(전전년) 값이 나란히 오게 한다
# shift 대신 merge: 중간 연도가 빠져 있어도 엉뚱한 해의 값이 붙지 않는다
def add_prior_years(data):
    prev = data[["stock_code", "fiscal_year", "fs_div", "ni", "assets",
                 "current_assets", "current_liabilities", "revenue",
                 "gross_profit", "cost_of_sales"]].copy()
    prev["fiscal_year"] = prev["fiscal_year"] + 1
    prev = prev.add_suffix("_prev").rename(columns={
        "stock_code_prev": "stock_code", "fiscal_year_prev": "fiscal_year"})

    prev2 = data[["stock_code", "fiscal_year", "fs_div", "assets"]].copy()
    prev2["fiscal_year"] = prev2["fiscal_year"] + 2
    prev2 = prev2.rename(columns={"fs_div": "fs_div_prev2", "assets": "assets_prev2"})

    out = data.merge(prev, on=["stock_code", "fiscal_year"], how="left")
    out = out.merge(prev2, on=["stock_code", "fiscal_year"], how="left")

    # 올해와 전년 또는 전전년의 기준(연결/별도)이 다르면 같은 잣대 비교가 아니다
    differs_prev = out["fs_div_prev"].notna() & (out["fs_div"] != out["fs_div_prev"])
    differs_prev2 = out["fs_div_prev2"].notna() & (out["fs_div"] != out["fs_div_prev2"])
    out["needs_same_basis"] = differs_prev | differs_prev2
    return out


# 동일기준 보완 파일: 한 줄에 값 하나
# "stock_code 회사의 score_year 점수를 계산할 때, 같은 기준(fs_div)의 value_fiscal_year 값은 amount"
def load_same_basis(path):
    df = read_csv_text(path, has_korean_header=False)
    out = df[["stock_code", "fs_div", "column"]].copy()
    out["score_year"] = df["score_year"].astype(int)
    out["value_fiscal_year"] = df["value_fiscal_year"].astype(int)
    out["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    return out


# 보완 파일의 값으로 _prev, _prev2 칸을 바꾼다. 02번 원본 값(assets, ni 등)은 그대로 둔다
def apply_same_basis_prior(data, same_basis):
    out = data.copy()
    out["prior_override_count"] = 0

    for row in same_basis.itertuples(index=False):
        years_back = row.score_year - row.value_fiscal_year       # 1이면 전년, 2면 전전년
        suffix = "_prev" if years_back == 1 else "_prev2"
        target = SAME_BASIS_COLUMNS[row.column] + suffix          # 예: assets → assets_prev

        mask = (
            (out["stock_code"] == row.stock_code)
            & (out["fiscal_year"] == row.score_year)
            & (out["fs_div"] == row.fs_div)
        )
        out.loc[mask, target] = row.amount
        out.loc[mask, "prior_override_count"] += 1

    # 기준이 섞였는데 보완값을 하나도 못 받은 행 (0개여야 정상)
    out["basis_unresolved"] = out["needs_same_basis"] & (out["prior_override_count"] == 0)
    return out



# 4. F1~F9 신호와 F-Score

# 조건을 0/1로 바꾸되, 원래 값이 빈칸이면 신호도 빈칸 (빈칸은 0점이 아니다)
def to_signal(condition, value):
    signal = condition.astype("Int64")
    signal[value.isna()] = pd.NA
    return signal


# 여러 신호가 같이 쓰는 값: 평균 총자산, 매출총이익
def add_base_values(df):
    out = df.copy()
    # 기초·기말 평균 총자산 (F5, F9의 분모)
    out["avg_assets"] = (out["assets"] + out["assets_prev"]) / 2
    out["avg_assets_prev"] = (out["assets_prev"] + out["assets_prev2"]) / 2
    # 매출총이익이 빈칸일 때만 매출 - 매출원가로 채운다
    out["gp"] = out["gross_profit"].fillna(out["revenue"] - out["cost_of_sales"])
    out["gp_prev"] = out["gross_profit_prev"].fillna(out["revenue_prev"] - out["cost_of_sales_prev"])
    return out


# F1. ROA > 0 → 1점 (이익을 냈는가). ROA = 순이익 / 기초 총자산
def add_f1_roa(df):
    out = df.copy()
    out["roa"] = out["ni"] / out["assets_prev"]
    out["f1_roa"] = to_signal(out["roa"] > 0, out["roa"])
    return out


# F2. 영업현금흐름 / 기초 총자산 > 0 → 1점 (영업으로 현금이 들어왔는가)
def add_f2_cfo(df):
    out = df.copy()
    out["cfo_ratio"] = out["cfo"] / out["assets_prev"]
    out["f2_cfo"] = to_signal(out["cfo_ratio"] > 0, out["cfo_ratio"])
    return out


# F3. ROA가 전년보다 상승 → 1점 (수익성이 개선됐는가). F1에 의존하지 않도록 ROA를 다시 계산
def add_f3_delta_roa(df):
    out = df.copy()
    roa = out["ni"] / out["assets_prev"]
    out["roa_prev"] = out["ni_prev"] / out["assets_prev2"]
    out["delta_roa"] = roa - out["roa_prev"]
    out["f3_delta_roa"] = to_signal(out["delta_roa"] > 0, out["delta_roa"])
    return out


# F4. (순이익 - 영업현금흐름) / 기초 총자산 < 0 → 1점 (이익이 현금으로 뒷받침되는가)
def add_f4_accrual(df):
    out = df.copy()
    out["accrual"] = (out["ni"] - out["cfo"]) / out["assets_prev"]
    out["f4_accrual"] = to_signal(out["accrual"] < 0, out["accrual"])
    return out


# F5. 장기부채 / 평균 총자산이 전년보다 하락 → 1점 (장기 빚 부담이 줄었는가)
# 예외: 당기말·전기말 장기부채가 모두 0이면 1점. 빚 부담이 줄지는 않았지만
# 무차입이라는 양호한 상태를 유지했기 때문. (원 논문 규칙은 변화가 0이라 0점)
def add_f5_delta_lever(df):
    out = df.copy()
    out["lever"] = out["ltd"] / out["avg_assets"]
    out["lever_prev"] = out["ltd_prior"] / out["avg_assets_prev"]
    out["delta_lever"] = out["lever"] - out["lever_prev"]
    out["f5_delta_lever"] = to_signal(out["delta_lever"] < 0, out["delta_lever"])
    out["f5_no_debt_both"] = (out["ltd"] == 0) & (out["ltd_prior"] == 0)   # 2년 연속 무차입
    out.loc[out["f5_no_debt_both"], "f5_delta_lever"] = 1
    out["f5_delta_lever"] = out["f5_delta_lever"].astype("Int64")
    return out


# F6. 유동비율이 전년보다 상승 → 1점 (단기 지급 능력이 좋아졌는가)
def add_f6_delta_liquid(df):
    out = df.copy()
    out["liquid"] = out["current_assets"] / out["current_liabilities"]
    out["liquid_prev"] = out["current_assets_prev"] / out["current_liabilities_prev"]
    out["delta_liquid"] = out["liquid"] - out["liquid_prev"]
    out["f6_delta_liquid"] = to_signal(out["delta_liquid"] > 0, out["delta_liquid"])
    return out


# F7. 그해 보통주 유상증자 없음 → 1점 (외부 자금 조달에 기대지 않았는가)
def add_f7_no_equity_offer(df):
    out = df.copy()
    out["f7_no_equity_offer"] = out["f7_score_official"].astype("Int64")
    return out


# F8. 매출총이익률이 전년보다 상승 → 1점 (매출 대비 남는 돈이 늘었는가). 영업이익률이 아님
def add_f8_delta_margin(df):
    out = df.copy()
    out["margin"] = out["gp"] / out["revenue"]
    out["margin_prev"] = out["gp_prev"] / out["revenue_prev"]
    out["delta_margin"] = out["margin"] - out["margin_prev"]
    out["f8_delta_margin"] = to_signal(out["delta_margin"] > 0, out["delta_margin"])
    return out


# F9. 매출 / 평균 총자산이 전년보다 상승 → 1점 (자산으로 매출을 더 잘 만드는가)
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


def add_signals(df):
    out = df
    for step in SIGNAL_STEPS:
        out = step(out)
    return out


# 9개 신호의 합 (0~9). 하나라도 빈칸이면 총점도 빈칸 (일부만 더한 점수는 비교할 수 없음)
def add_fscore(df):
    out = df.copy()
    out["f_score"] = out[SIGNAL_COLUMNS].sum(axis=1, min_count=len(SIGNAL_COLUMNS)).astype("Int64")
    return out


# 5. F7 대조 
# 09번: 발행 한 건이 한 줄
def load_issues(path):
    df = read_csv_text(path, has_korean_header=True)
    out = pd.DataFrame({
        "stock_code": df["stock_code"],
        "issue_date": pd.to_datetime(df["isu_dcrs_de"], format="%Y.%m.%d", errors="coerce"),
        "issue_type": df["isu_dcrs_stle"],
        "stock_kind": df["isu_dcrs_stock_knd"],
        "quantity": pd.to_numeric(df["isu_dcrs_qy"].str.replace(",", ""), errors="coerce"),
    })
    out = out.dropna(subset=["issue_date"])                    
    out = out.drop_duplicates(subset=["stock_code", "issue_date", "issue_type", "stock_kind", "quantity"])
    out["fiscal_year"] = out["issue_date"].dt.year              # 발행일 연도 = 회계연도 (12월 결산)
    return out


# 보통주 유상증자가 있었던 회사·연도 목록
def own_equity_offer_years(issues):
    is_common = issues["stock_kind"] == "보통주"
    is_offer = issues["issue_type"].str.contains("유상증자")
    offers = issues[is_common & is_offer]
    years = offers[["stock_code", "fiscal_year"]].drop_duplicates()
    years["issued_own"] = True
    return years


# 두 판정이 다른 회사·연도를 돌려준다 (비어 있으면 일치)
def compare_f7(data, own_years):
    out = data.merge(own_years, on=["stock_code", "fiscal_year"], how="left")
    out["issued_own"] = out["issued_own"].fillna(False).astype(bool)   # 기록이 없으면 발행 없음
    out["f7_own"] = (~out["issued_own"]).astype(int)                     # 발행 없음 → 1점
    diff = out[out["f7_own"] != out["f7_no_equity_offer"]]
    return diff[["company_name", "fiscal_year", "f7_own", "f7_no_equity_offer", "f7_status"]]



# 6. 매수일·평가일 비교

# 그날까지 공시된 사업보고서 중 가장 최근 것의 F-Score (미래 정보 차단)
def find_fscore_as_of(df, stock_code, date):
    date = pd.Timestamp(date)
    rows = df[(df["stock_code"] == stock_code) & (df["disclosed_date"] <= date)]
    if rows.empty:
        return None
    return rows.sort_values("disclosed_date").iloc[-1]


# 1차: 하락폭을 0~2점으로 바꾼다 (RISK_SCALE 표 기준). 하락폭이 없으면 점수도 없다
def map_risk_score(f_score_drop):
    if pd.isna(f_score_drop):
        return pd.NA
    for min_drop, risk in RISK_SCALE:
        if f_score_drop >= min_drop:
            return risk
    return pd.NA


# 2차: 평가일 F-Score가 0~1점이면 +1점, 아니면 0점
def level_add_points(f_score_eval):
    if pd.isna(f_score_eval):
        return pd.NA
    return LEVEL_ADD_POINTS if f_score_eval <= LEVEL_ADD_MAX_SCORE else 0


# 종목 하나의 매수일과 평가일(현재 날짜)을 비교한다. 문제가 있으면 사유를 남기고 그 자리에서 끝낸다
def evaluate_pair(df, stock_code, buy_date, eval_date):
    out = {"stock_code": stock_code, "buy_date": buy_date, "eval_date": eval_date,
           "f_score_drop": None, "risk_drop": None, "risk_level_add": None, "risk_score": None}
    hold = []

    # 1) 상장폐지 이후 날짜는 분석 대상 아님
    if stock_code in DELISTED:
        delist_date = pd.Timestamp(DELISTED[stock_code])
        if pd.Timestamp(buy_date) > delist_date or pd.Timestamp(eval_date) > delist_date:
            hold.append("after_delisting")

    # 2) 두 날짜의 F-Score 찾기
    buy = find_fscore_as_of(df, stock_code, buy_date)
    now = find_fscore_as_of(df, stock_code, eval_date)
    if buy is None:
        hold.append("buy_no_data")
    if now is None:
        hold.append("eval_no_data")
    if hold:
        out["hold_reason"] = "|".join(hold)
        return out

    # 3) 근거 보고서와 점수 기록 (f_score_buy = 매수일 점수, f_score_eval = 평가일 점수)
    out["fiscal_year_buy"] = buy["fiscal_year"]
    out["fiscal_year_eval"] = now["fiscal_year"]
    out["f_score_buy"] = buy["f_score"]
    out["f_score_eval"] = now["f_score"]
    out["rcept_no_buy"] = buy["rcept_no"]
    out["rcept_no_eval"] = now["rcept_no"]
    out["same_report"] = buy["fiscal_year"] == now["fiscal_year"]   # 같은 보고서면 하락폭은 항상 0

    # 4) 비교: 하락폭 = 매수일 점수 - 평가일 점수 (양수 = 하락 = 위험 증가)
    out["f_score_drop"] = buy["f_score"] - now["f_score"]
    out["hold_reason"] = ""

    # 5) 같은 보고서는 새 정보가 없어 비교를 못 한 것이므로 위험점수를 유보한다 (0점이 아님)
    if HOLD_IF_SAME_REPORT and out["same_report"]:
        out["hold_reason"] = "same_report"
        return out

    # 6) 위험점수 = 하락폭 점수(0~2) + 평가일 수준 점수(0~1)
    out["risk_drop"] = map_risk_score(out["f_score_drop"])
    out["risk_level_add"] = level_add_points(out["f_score_eval"])
    out["risk_score"] = out["risk_drop"] + out["risk_level_add"]
    return out


def evaluate_pairs(df, pairs):
    rows = [evaluate_pair(df, stock_code, buy_date, eval_date)
            for stock_code, buy_date, eval_date in pairs]
    table = pd.DataFrame(rows)
    for col in ["f_score_drop", "risk_drop", "risk_level_add", "risk_score",
                "fiscal_year_buy", "fiscal_year_eval", "f_score_buy", "f_score_eval"]:
        table[col] = table[col].astype("Int64")
    table["risk_scale_status"] = "confirmed" if RISK_SCALE_CONFIRMED else "provisional"
    return table


# 보고서 연도 조합별 하락폭과 위험점수 (분포 확인용). 상장폐지 이후 보고서는 제외
# 같은 연도끼리는 조합에서 빠지므로 같은 보고서 유보와는 상관없다
def report_pair_deltas(df):
    scores = df[["stock_code", "fiscal_year", "disclosed_date", "f_score"]]
    pairs = scores.merge(scores, on="stock_code", suffixes=("_buy", "_eval"))
    pairs = pairs[pairs["fiscal_year_eval"] > pairs["fiscal_year_buy"]]
    for code, date in DELISTED.items():
        after = (pairs["stock_code"] == code) & (pairs["disclosed_date_eval"] > pd.Timestamp(date))
        pairs = pairs[~after]
    pairs["gap"] = pairs["fiscal_year_eval"] - pairs["fiscal_year_buy"]
    pairs["f_score_drop"] = pairs["f_score_buy"] - pairs["f_score_eval"]
    pairs["risk_drop"] = pairs["f_score_drop"].map(map_risk_score).astype("Int64")
    pairs["risk_level_add"] = pairs["f_score_eval"].map(level_add_points).astype("Int64")
    pairs["risk_score"] = pairs["risk_drop"] + pairs["risk_level_add"]
    return pairs


def build_table():
    annual = load_annual(ANNUAL_FILE)
    ltd = load_ltd(LTD_FILE)
    data = combine(annual, ltd)
    data = attach_f7(data, load_f7(F7_FILE))
    data = add_prior_years(data)
    data = apply_same_basis_prior(data, load_same_basis(SAME_BASIS_FILE))
    data = add_signals(data)
    data = data[data["fiscal_year"] >= FIRST_SCORE_YEAR].copy()
    data = add_fscore(data)
    return data

def fscore_compare(table, stock_code, buy_date, now_date):
    return evaluate_pair(table, stock_code, buy_date, now_date)


if __name__ == "__main__":
    table = build_table()
    print(fscore_compare(table, "010170", "2016-06-30", "2019-06-28")["risk_score"])


"""
# 실행

if __name__ == "__main__":
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", None)
    pd.set_option("display.float_format", "{:,.2f}".format)

    # ---- 1. 읽기 ----
    annual = load_annual(ANNUAL_FILE)
    ltd = load_ltd(LTD_FILE)
    data = combine(annual, ltd)

    # ---- 2. F7 판정 붙이기 ----
    data = attach_f7(data, load_f7(F7_FILE))

    # ---- 3. 전년·전전년 값, 같은 기준 과거값 ----
    data = add_prior_years(data)
    data = apply_same_basis_prior(data, load_same_basis(SAME_BASIS_FILE))

    # ---- 4. 신호와 F-Score ----
    data = add_signals(data)
    data = data[data["fiscal_year"] >= FIRST_SCORE_YEAR].copy()
    data = add_fscore(data)

    # ---- 5. 확인 ----
    print("[확인]")
    print("계산 대상 행:", len(data), "(기대 95 = 19종목 × 5년)")
    print("30번 기준 불일치:", (~data["ltd_fs_div_match"]).sum(), "(기대 0)")
    print("같은 기준 보완 필요 행:", data["needs_same_basis"].sum())
    print("보완값을 못 받은 행:", data["basis_unresolved"].sum(), "(기대 0)")
    print("F-Score 빈칸:", data["f_score"].isna().sum(), "(기대 0)")
    print("F7=0 (유상증자 있음):", (data["f7_no_equity_offer"] == 0).sum(), "(기대 20)")
    print("F5 예외 해당 행 (2년 연속 장기부채 0 → F5 1점):", data["f5_no_debt_both"].sum())
    print()
    print("[같은 기준 과거값으로 바꾼 행]")
    print(data[data["prior_override_count"] > 0][
        ["company_name", "fiscal_year", "fs_div", "fs_div_prev", "fs_div_prev2", "prior_override_count", "f_score"]])

    if ISSUE_FILE.exists():
        diff = compare_f7(data, own_equity_offer_years(load_issues(ISSUE_FILE)))
        print()
        print("[F7 대조: 09번 직접 판정 vs 데이터 담당 판정] 다른 행:", len(diff))
        if len(diff) > 0:
            print(diff)

    print()
    print("[회사 × 연도 F-Score]")
    print(data.pivot(index="company_name", columns="fiscal_year", values="f_score"))

    # ---- 6. 매수일·평가일 비교 (테스트) ----
    test_pairs = [
        ("010170", "2016-06-30", "2019-06-28"),   # 대한광통신: 2015 보고서 → 2018 보고서
        ("036890", "2016-06-30", "2019-06-28"),   # 진성티이씨: 2015 보고서 → 2018 보고서
        ("010170", "2017-06-30", "2017-12-28"),   # 대한광통신: 두 날짜 모두 2016 보고서
        ("052270", "2016-06-30", "2019-06-28"),   # 우전앤한단: 평가일이 상장폐지 이후
        ("010170", "2015-06-30", "2016-06-30"),   # 대한광통신: 매수일에 2015 보고서가 아직 없음
    ]
    eval_table = evaluate_pairs(data, test_pairs)
    print()
    print("[매수일·평가일 테스트]")
    print(eval_table[["stock_code", "buy_date", "eval_date", "fiscal_year_buy", "fiscal_year_eval",
                      "f_score_buy", "f_score_eval", "f_score_drop", "risk_drop", "risk_level_add",
                      "risk_score", "same_report", "hold_reason"]])

    # ---- 7. 하락폭 분포와 위험점수 분포 ----
    deltas = report_pair_deltas(data)
    print()
    print("[보유 기간별 하락폭 (매수일 - 평가일)] 조합 수:", len(deltas))
    print(deltas.groupby("gap")["f_score_drop"].describe())
    print()
    print("[위험점수(0~3) 분포] 1차 눈금:", RISK_SCALE, "/ 평가일", LEVEL_ADD_MAX_SCORE, "점 이하 +", LEVEL_ADD_POINTS)
    print(deltas["risk_score"].value_counts().sort_index().to_string())
    print()
    print("[보유 기간별 위험점수 분포]")
    print(deltas.groupby(["gap", "risk_score"]).size().unstack(fill_value=0))
    print()
    print("[평가일 F-Score 0~1점인 조합]", (deltas["f_score_eval"] <= LEVEL_ADD_MAX_SCORE).sum())

    # ---- 8. 저장 ----
    OUTPUT_DIR.mkdir(exist_ok=True)
    keep = ["stock_code", "company_name", "fiscal_year", "fs_div", "rcept_no", "disclosed_date",
            "prior_override_count"] + SIGNAL_COLUMNS + ["f_score"]
    data[keep].to_csv(OUTPUT_DIR / "fscore_annual.csv", index=False, encoding="utf-8-sig")
    eval_table.to_csv(OUTPUT_DIR / "fscore_eval.csv", index=False, encoding="utf-8-sig")
    print()
    print("저장 완료:", OUTPUT_DIR)


"""
