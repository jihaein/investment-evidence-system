"""
build_ohlson_input.py — 본 데이터로 Ohlson 일별 입력표 만들기

[무엇을 하나]
분기 재무(04번)와 일별 시가총액(05번)을 합쳐, ohlson.py가 그대로 읽을 수 있는
"회사 × 거래일" 입력표를 만든다.

    매일의 행 = 그날 시가총액(05번)
              + 그날까지 쓸 수 있게 된(available_from) 가장 최근 분기의 재무값(04번)

[데이터]
    04_재무_Ohlson용_2015-2019.csv   분기 지배주주 자본·순이익
    05_주가_일별_2015-2019.csv       일별 시가총액
    04A_Ohlson_연간_2015-2019.csv    연간 지배주주 값

[04번 규칙] 
    - 순이익은 "분기 단독" 금액이다. TTM은 이 코드에서 최근 4개 분기를 더해서 만든다
    - 분기마다 연결(CFS) 우선, 연결 자료가 없으면 별도(OFS)
    - 빈칸은 0이 아니라 "원천에서 값을 찾지 못함"

    TTM 4개 분기에 연결·별도가 섞이면: 섞어서 계산하고 표시만 남긴다
      - "연결이 없으면 별도" 규칙을 분기마다 적용한 결과를 그대로 더한다
      - 예: 지엔코는 매년 3분기 연결 API 자료가 없어 3·4분기가 별도 → 유보하면 2016Q3 이후 전부 빠짐
      - 섞인 행은 ttm_basis_mixed = True로 표시해서, 나중에 빼고도 결과가 같은지 비교할 수 있게 한다
      - ALLOW_MIXED_BASIS_TTM = False로 바꾸면 섞인 행을 유보한다

[만드는 값]
    ni_ttm_used  최근 4개 분기 지배주주 순이익의 합
    bv_t         이번 분기말 지배주주 자본
    bv_prev      TTM 기간이 시작할 때의 자본 (기본: 4분기 전 같은 분기말)

[실행 순서]
    04번 읽기 → 이전 분기 값 붙이기 → TTM·BV(전기) 계산 → (04A 대조)
    → 05번 읽기 → 거래일마다 최근 분기 값 붙이기 → 확인 → 저장
"""

from pathlib import Path
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data" / "ohlson"

QUARTERLY_FILE = DATA_DIR / "04_재무_Ohlson용_2015-2019.csv"
PRICE_FILE = DATA_DIR / "05_주가_일별_2015-2019.csv"
ANNUAL_FILE = DATA_DIR / "04A_Ohlson_연간_2015-2019.csv"      # 대조용
OUTPUT_FILE = DATA_DIR / "ohlson_input_daily.csv"

CORRECTION_FILE = DATA_DIR / "04_수정내역.csv"

# BV(전기)를 무엇으로 볼지
#   "same_quarter_last_year": 4분기 전 같은 분기말 자본 (TTM 기간이 시작할 때의 자본. 기본값)
#   "prior_fy_end":           직전 사업연도 말 자본
BV_PREV_MODE = "same_quarter_last_year"

# TTM 4개 분기(또는 BV 비교)에 연결·별도가 섞일 때
#   True:  분기마다 그 분기 기준 값을 그대로 쓰고, ttm_basis_mixed 표시만 남긴다
#   False: 계산하지 않고 유보한다 (계약서 "한 계산에 섞지 않음")
ALLOW_MIXED_BASIS_TTM = True

DELISTED = {"052270": "2017-04-12"}     # 상장폐지 종목과 폐지일 (우전앤한단)

QUARTERLY_COLUMNS = {
    "ifrs_EquityAttributableToOwnersOfParent_thstrm_amount": "bv_t",            # 이번 분기말 지배주주 자본
    "ifrs_EquityAttributableToOwnersOfParent_prior_fy_end": "bv_prior_fy_end",  # 직전 사업연도 말 지배주주 자본
    "ifrs_ProfitLossAttributableToOwnersOfParent_quarter_amount": "ni_q",       # 이번 분기 단독 지배주주 순이익
}

# 1. read data
def read_csv_text(path, code_column):
    df = pd.read_csv(path, header=0, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    if len(df) > 0 and not df[code_column].iloc[0].strip().isdigit():
        df = df.iloc[1:].reset_index(drop=True)
    return df


# 04번: 회사 × 분기 한 줄
def load_quarterly(path):
    df = read_csv_text(path, "stock_code")
    out = pd.DataFrame({
        "stock_code": df["stock_code"],
        "corp_code": df["corp_code"],
        "company_name": df["stock_name_2014"],
        "fs_div": df["fs_div"],
        "bsns_year": df["bsns_year"].astype(int),
        "quarter": df["quarter"].astype(int),
        "rcept_no": df["rcept_no"],
        "owner_value_source": df["owner_value_source"],
        # 이 분기 값을 처음 쓸 수 있는 날
        "available_from": pd.to_datetime(df["available_from"], format="%Y%m%d", errors="coerce"),
    })
    for source, name in QUARTERLY_COLUMNS.items():
        out[name] = pd.to_numeric(df[source], errors="coerce")
    # 분기 순서 번호: 2015Q1 = 8061, 2015Q2 = 8062 … (n분기 전 = 번호 - n)
    out["seq"] = out["bsns_year"] * 4 + out["quarter"]
    return out.sort_values(["stock_code", "seq"]).reset_index(drop=True)


# 수정 내역 파일의 값으로 04번 분기 순이익을 바꾼다
# 04번 원본 CSV는 그대로 두고, 읽어온 표에서만 바꾼다 (데이터 담당이 새 04를 보내도 수정이 유지됨)
# before 값이 지금 값과 다르면 바꾸지 않고 경고한다 (새 04에서 이미 고쳐졌을 수 있으므로)
def apply_corrections(q, path):
    out = q.copy()
    out["ni_q_corrected"] = False
    fixes = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")

    for fix in fixes.itertuples(index=False):
        mask = (
            (out["stock_code"] == fix.stock_code)
            & (out["bsns_year"] == int(fix.bsns_year))
            & (out["quarter"] == int(fix.quarter))
        )
        if mask.sum() != 1:
            print(f"  [건너뜀] {fix.stock_code} {fix.bsns_year}Q{fix.quarter}: 해당 행을 찾지 못함")
            continue

        current = out.loc[mask, "ni_q"].iloc[0]
        if current != float(fix.before):
            print(f"  [건너뜀] {fix.stock_code} {fix.bsns_year}Q{fix.quarter}: "
                  f"지금 값 {current:,.0f}이 before {float(fix.before):,.0f}와 다름 (이미 고쳐졌을 수 있음)")
            continue

        out.loc[mask, "ni_q"] = float(fix.after)
        out.loc[mask, "ni_q_corrected"] = True
        print(f"  [수정] {fix.stock_code} {fix.bsns_year}Q{fix.quarter}: "
              f"{float(fix.before):,.0f} → {float(fix.after):,.0f}")
    return out


# 05번: 회사 × 거래일 시가총액
def load_prices(path):
    df = read_csv_text(path, "ISU_CD")
    return pd.DataFrame({
        "stock_code": df["ISU_CD"],
        "trade_date": pd.to_datetime(df["BAS_DD"], format="%Y%m%d"),
        "mktcap": pd.to_numeric(df["MKTCAP"], errors="coerce"),
    })


# 2. 이전 분기 값 붙이기와 TTM·BV(전기) 계산

# n분기 전 값을 옆에 붙인다 (fscore.py의 전년 값 붙이기와 같은 방식)
# shift 대신 merge: 중간 분기가 빠져 있어도 엉뚱한 분기 값이 붙지 않는다
def add_lag(q, n, columns):
    lag = q[["stock_code", "seq"] + columns].copy()
    lag["seq"] = lag["seq"] + n
    lag = lag.rename(columns={c: f"{c}_lag{n}" for c in columns})
    return q.merge(lag, on=["stock_code", "seq"], how="left")


# 최근 4개 분기 순이익 합(TTM)과 BV(전기)를 만든다
def add_ttm_and_bv_prev(q, bv_prev_mode, allow_mixed):
    out = q.copy()
    for n in [1, 2, 3]:
        out = add_lag(out, n, ["ni_q", "fs_div"])
    out = add_lag(out, 4, ["bv_t", "fs_div"])

    # ---- NI TTM ----
    ni_cols = ["ni_q", "ni_q_lag1", "ni_q_lag2", "ni_q_lag3"]
    all_present = out[ni_cols].notna().all(axis=1)          # 4개 분기가 모두 있는가
    same_basis = (                                           # 4개 분기가 모두 같은 기준인가
        (out["fs_div_lag1"] == out["fs_div"])
        & (out["fs_div_lag2"] == out["fs_div"])
        & (out["fs_div_lag3"] == out["fs_div"])
    )
    ttm_sum = out[ni_cols].sum(axis=1)
    if allow_mixed:
        out["ni_ttm"] = ttm_sum.where(all_present)                   # 섞여도 계산
    else:
        out["ni_ttm"] = ttm_sum.where(all_present & same_basis)      # 섞이면 빈칸

    # 섞였는지는 스위치와 상관없이 항상 기록 (나중에 빼고 비교할 수 있게)
    out["ttm_basis_mixed"] = all_present & ~same_basis

    out["ttm_reason"] = ""
    out.loc[~all_present, "ttm_reason"] = "ttm_quarter_missing"          # 4개 분기 중 빠진 것이 있음
    if not allow_mixed:
        out.loc[all_present & ~same_basis, "ttm_reason"] = "ttm_basis_mixed"

    # ---- BV(전기) ----
    if bv_prev_mode == "prior_fy_end":
        out["bv_prev"] = out["bv_prior_fy_end"]
        out["bv_prev_source"] = "prior_fy_end"
        out["bv_prev_basis_mixed"] = False                   # 같은 보고서 안의 값이라 항상 같은 기준
    else:
        # 4분기: "4분기 전 같은 분기말" = "직전 사업연도 말"이라 이번 보고서의 전기말 값을 쓴다
        #        (같은 보고서 안의 값이라 기준이 항상 같다. 2015년 4분기도 계산 가능)
        # 1~3분기: 4분기 전 행의 분기말 자본을 쓴다
        is_q4 = out["quarter"] == 4
        lag4_present = out["bv_t_lag4"].notna()
        lag4_same = out["fs_div_lag4"] == out["fs_div"]
        lag4_ok = lag4_present if allow_mixed else (lag4_present & lag4_same)

        out["bv_prev"] = out["bv_prior_fy_end"].where(is_q4, out["bv_t_lag4"].where(lag4_ok))
        out["bv_prev_source"] = "prior_fy_end_q4"
        out.loc[~is_q4, "bv_prev_source"] = "same_quarter_last_year"
        out["bv_prev_basis_mixed"] = ~is_q4 & lag4_present & ~lag4_same

    out["bv_reason"] = ""
    out.loc[out["bv_prev"].isna(), "bv_reason"] = "bv_prev_missing"
    return out


# 3. 거래일마다 최근 분기 값 붙이기

# 각 거래일에, 그날 이전(같은 날 포함)부터 쓸 수 있게 된 가장 최근 분기 값을 붙인다
# direction="backward" → 미래에 공시된 값은 절대 붙지 않는다
def build_daily(prices, q):
    q_ready = q.dropna(subset=["available_from"])
    merged = pd.merge_asof(
        prices.sort_values("trade_date"),
        q_ready.sort_values("available_from"),
        left_on="trade_date",
        right_on="available_from",
        by="stock_code",
        direction="backward",
    )
    merged = merged.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)

    # ---- ohlson.py가 읽는 컬럼 이름으로 맞춘다 ----
    daily = pd.DataFrame({
        "trade_date": merged["trade_date"].dt.strftime("%Y-%m-%d"),
        "stock_code": merged["stock_code"],
        "corp_code": merged["corp_code"],
        "company_name": merged["company_name"],
        "fs_div_used": merged["fs_div"],                    # 이번 분기 값의 기준 (연결/별도)
        "period_fiscal_year": merged["bsns_year"].astype("Int64"),
        "period_quarter": merged["quarter"].astype("Int64"),
        "ni_ttm_used": merged["ni_ttm"],
        "ni_source": "quarter_sum_ttm",
        "ni_receipt": merged["rcept_no"],
        "ni_available_from": merged["available_from"].dt.strftime("%Y-%m-%d"),
        "bv_t": merged["bv_t"],
        "bv_t_source": merged["owner_value_source"],        # 지배주주 값 출처
        "bv_t_receipt": merged["rcept_no"],
        "bv_prev": merged["bv_prev"],
        "bv_prev_source": merged["bv_prev_source"],
        "bv_prev_receipt": merged["rcept_no"],
        "mktcap": merged["mktcap"],
        "list_shrs": pd.NA,
        # 연결·별도가 섞인 계산인지 
        "ttm_basis_mixed": merged["ttm_basis_mixed"].fillna(False).astype(bool),
        "bv_prev_basis_mixed": merged["bv_prev_basis_mixed"].fillna(False).astype(bool),
    })

    # ---- 빈 이유 (여러 개면 | 로 이어 붙임) ----
    reasons = pd.Series("", index=daily.index)

    def add_reason(mask, name):
        nonlocal reasons                 # 바깥 함수의 reasons를 고친다
        reasons = reasons.where(~mask, reasons + name + "|")

    has_quarter = merged["available_from"].notna()
    add_reason(~has_quarter, "no_quarter_available_yet")
    add_reason(merged["ttm_reason"].fillna("") != "", merged["ttm_reason"].fillna(""))
    add_reason((merged["bv_reason"].fillna("") != "") & has_quarter, "bv_prev_missing")
    add_reason(merged["bv_t"].isna() & has_quarter, "bv_t_missing")
    add_reason(merged["mktcap"].isna(), "mktcap_missing")
    for code, date in DELISTED.items():
        add_reason((daily["stock_code"] == code) & (merged["trade_date"] > pd.Timestamp(date)), "after_delisting")
    daily["missing_reason"] = reasons.str.rstrip("|")

    # ---- 사용 가능 여부와 자본잠식 ----
    ready = daily[["ni_ttm_used", "bv_t", "bv_prev", "mktcap"]].notna().all(axis=1) & (daily["missing_reason"] == "")
    daily["input_ready"] = ready.map({True: "true", False: "false"})
    daily["input_ready_strict"] = daily["input_ready"]     # ohlson.py가 이 컬럼으로 거른다
    impaired = (daily["bv_t"] <= 0) | (daily["bv_prev"] <= 0)
    daily["capital_impairment"] = impaired.map({True: "Y", False: ""})
    daily["substitution_risk"] = ""
    return daily


# 4. 대조: 4분기 TTM = 연간 순이익 이어야 한다
# 4분기 TTM은 1~4분기를 다 더한 값이라 그해 연간 순이익과 같아야 한다
# 1원 넘게 다른 행만 돌려준다. 04A와 기준(연결/별도)이 다른지도 표시한다
def compare_q4_with_annual(q, annual_path):
    df = read_csv_text(annual_path, "stock_code")
    annual = pd.DataFrame({
        "stock_code": df["stock_code"],
        "bsns_year": df["bsns_year"].astype(int),
        "annual_fs_div": df["fs_div"],
        "ni_annual": pd.to_numeric(df["ifrs_ProfitLossAttributableToOwnersOfParent_thstrm_amount"], errors="coerce"),
    })
    q4 = q[q["quarter"] == 4][["stock_code", "company_name", "bsns_year", "fs_div",
                               "ni_ttm", "ttm_basis_mixed"]]
    both = q4.merge(annual, on=["stock_code", "bsns_year"], how="left")
    both["diff"] = both["ni_ttm"] - both["ni_annual"]
    both["diff_pct"] = (both["diff"] / both["ni_annual"].abs() * 100).round(2)

    # 차이의 원인 분류
    both["cause"] = "check_needed"                                             # 원인 미확인
    both.loc[both["diff_pct"].abs() < 1, "cause"] = "small"                    # 1% 미만
    both.loc[both["fs_div"] != both["annual_fs_div"], "cause"] = "annual_basis_differs"   # 04A와 기준 다름
    both.loc[both["ttm_basis_mixed"], "cause"] = "ttm_basis_mixed"            # 분기끼리 기준 섞임

    diff = both[both["ni_ttm"].notna() & both["ni_annual"].notna() & (both["diff"].abs() > 1)]
    return diff[["company_name", "bsns_year", "fs_div", "annual_fs_div",
                 "ni_ttm", "ni_annual", "diff", "diff_pct", "cause"]]


# 실행

if __name__ == "__main__":
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", None)
    pd.set_option("display.float_format", "{:,.0f}".format)

    # ---- 1. 04번 ----
    q = load_quarterly(QUARTERLY_FILE)
    print("[04번 분기 재무]")
    print("행 수:", len(q), "(기대 380 = 19종목 × 20분기)")
    print("종목 수:", q["stock_code"].nunique(), "(기대 19)")
    print("중복 (종목+연도+분기):", q.duplicated(["stock_code", "bsns_year", "quarter"]).sum(), "(기대 0)")
    print("available_from 빈칸:", q["available_from"].isna().sum())

    # ---- 1-1. 우리가 확인한 수정 적용 ----
    if CORRECTION_FILE.exists():
        print()
        print("[수정 내역 적용]")
        q = apply_corrections(q, CORRECTION_FILE)

    # ---- 2. TTM·BV(전기) ----
    q = add_ttm_and_bv_prev(q, BV_PREV_MODE, ALLOW_MIXED_BASIS_TTM)
    print()
    print("[TTM·BV(전기)] BV 기준:", BV_PREV_MODE, "/ 기준 섞임 허용:", ALLOW_MIXED_BASIS_TTM)
    print("TTM 계산됨:", q["ni_ttm"].notna().sum(), "/", len(q))
    print("TTM 빈칸 이유:")
    print(q.loc[q["ttm_reason"] != "", "ttm_reason"].value_counts().to_string())
    print("TTM에 연결·별도가 섞인 분기:", q["ttm_basis_mixed"].sum())
    print("BV(전기)에 연결·별도가 섞인 분기:", q["bv_prev_basis_mixed"].sum())
    print("BV(전기) 빈칸:", q["bv_prev"].isna().sum())

    if ANNUAL_FILE.exists():
        diff = compare_q4_with_annual(q, ANNUAL_FILE)
        print()
        print("[대조] 4분기 TTM ≠ 04A 연간 순이익:", len(diff), "건")
        print(diff["cause"].value_counts().to_string())
        print()
        print(diff[diff["cause"] == "check_needed"])
        if len(diff) > 0:
            print(diff)
    else:
        print()
        print("[대조] 04A 파일이 없어 건너뜀")

    # ---- 3. 05번과 합치기 ----
    prices = load_prices(PRICE_FILE)
    daily = build_daily(prices, q)

    print()
    print("[일별 입력표]")
    print("05번 행 수:", len(prices), "(기대 22,650)")
    print("입력표 행 수:", len(daily), "(05번과 같아야 함)")
    ready = daily["input_ready_strict"] == "true"
    print("사용 가능 행:", ready.sum())
    print("  그중 TTM 연결·별도 섞임:", (ready & daily["ttm_basis_mixed"]).sum())
    print("  그중 BV(전기) 연결·별도 섞임:", (ready & daily["bv_prev_basis_mixed"]).sum())
    print("사용 가능 첫 날짜:", daily.loc[ready, "trade_date"].min())
    print("자본잠식 행:", (daily["capital_impairment"] == "Y").sum())
    print()
    print("[빈 이유별 행 수]")
    print(daily.loc[~ready, "missing_reason"].value_counts().to_string())

    # 손계산 대조: 대한광통신 2015년 사업보고서 공시 전후
    check = daily[(daily["stock_code"] == "010170")
                  & (daily["trade_date"].between("2016-03-29", "2016-04-01"))]
    print()
    print("[대한광통신 2016-03-29 ~ 04-01]")
    print(check[["trade_date", "period_fiscal_year", "period_quarter", "ni_ttm_used",
                 "bv_t", "bv_prev", "mktcap", "input_ready_strict", "missing_reason"]])

    # ---- 4. 저장 ----
    daily.to_csv(OUTPUT_FILE, index=False, encoding="utf-8-sig")
    print()
    print("저장 완료:", OUTPUT_FILE)