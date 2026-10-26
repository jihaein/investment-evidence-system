# ohlson.py

# 밤티 코드 주의

# read data from csv and filter the data for strict input_ready
from pathlib import Path
import pandas as pd

INPUT_PATH = Path(r"C:\Users\SD1-06\Downloads\ohlson_input_final.csv")

TEXT_COLUMNS = ["stock_code", "corp_code", "ni_receipt", "bv_t_receipt", "bv_prev_receipt"]
NUMBER_COLUMNS = ["ni_ttm_used", "bv_t", "bv_prev", "mktcap", "list_shrs"]

def load_input(path):
    df = pd.read_csv(path, dtype={col: str for col in TEXT_COLUMNS}, encoding="utf-8-sig")
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for col in NUMBER_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["input_ready_strict"] = df["input_ready_strict"].astype(str).str.lower().eq("true")
    return df

def filter_strict(df):
    return df[df["input_ready_strict"]].copy()

# calcuate RI, Prem, ω
def add_ohlson_values(df, r, vt):
    out = df.copy()
    out["r_used"] = r

    # RI: residual income, 잔여이익 = NI - r * BV_prev 
    out["residual_income"] = out["ni_ttm_used"] - r * out["bv_prev"]

    # Prem: 시가총액 - BV(최근) - vt
    out["market_premium"] = out["mktcap"] - out["bv_t"] - vt

    denominator = out["market_premium"] + out["residual_income"]

    # ω = (1 + r) × Prem / (Prem + RI)
    out["omega"] = (1 + r) * out["market_premium"] / denominator
    return out


# 310 2016-04-05        대한광통신 -7,662,209,199.4900 -13,284,337,276.0000 0.6881
# 311 2016-04-06        대한광통신 -7,662,209,199.4900  -9,057,750,706.0000 0.5878
# prem이 -132억 -> -90억으로 좋아짐 but ω 0.69 -> 0.59로 떨어짐.
# RI와 Prem이 모두 음수일 때 ω 방향이 거꾸로 움직이는 문제
# 해석 가능 여부 판정 추가 필요

# flag each condition
def add_flags(df, omega_max):
    out = df.copy()
    denominator = out["market_premium"] + out["residual_income"]

    # 조건: 자본잠식(BV가 0 이하라 prem 자체가 비정상)
    out["flag_capital_impairment"] = out["capital_impairment"].eq("Y")

    # 조건: 잔여이익이 음수(RI < 0)
    out["flag_ri_negative"] = out["residual_income"] < 0

    # 조건: prem < 0 (시총이 자본보다 작아서 ω방향이 뒤집힐 수 있음)
    out["flag_prem_negative"] = out["market_premium"] < 0

    # 조건: prem+ri =< 0 (ω식의 분모가 0이하)
    out["flag_denominator_nonpositive"] = denominator <= 0

    # 조건: ω가 0보다 작거나 omega_max 이상 (ω가 0~1 범위를 벗어남)
    # 상한을 1로 할지, 1+r로 할지? (아직 미정) -> 일단 omega_max로 설정
    out["flag_omega_out_of_range"] = (out["omega"] < 0) | (out["omega"] >= omega_max)
    out["flag_not_strict"] = ~out["input_ready_strict"]
    return out

# apply hold rules to decide interpretability
def decide_interpretable(df, hold_rules):
    out = df.copy()
    out["omega_interpretable"] = ~out[hold_rules].any(axis=1)

    out["hold_reason"] = ""
    for col in hold_rules:
        name = col.replace("flag_", "")
        mask = out[col]
        out.loc[mask, "hold_reason"] = out.loc[mask, "hold_reason"] + name + "|"
    out["hold_reason"] = out["hold_reason"].str.rstrip("|")
    return out


# RI < 0이면 ω는 해석 불가 (초과이익이 없으니 지속성을 물을 수 없음)
# strict 16,338행 중 4,092행 해석 가능

# find the row on the date
# or the last trading day before it
def find_row_on_or_before(df, stock_code, date, max_gap_days):
    date = pd.Timestamp(date)
    rows = df[(df["stock_code"] == stock_code) & (df["trade_date"] <= date)]
    if rows.empty:
        return None
    row = rows.sort_values("trade_date").iloc[-1]
    if (date - row["trade_date"]).days > max_gap_days:
        return None
    return row


# classify a change as up, down, flat
def direction(change, tolerance):
    if change > tolerance:
        return "up"
    if change < -tolerance:
        return "down"
    return "flat"

# score from the directions of omega and ri
# if no rule maches -> none
def score_from_directions(omega_dir, ri_dir):
    if omega_dir == "up" and ri_dir == "up":
        return 0
    if omega_dir == "flat" and ri_dir == "down":
        return 1
    if omega_dir == "down" and ri_dir == "down":
        return 2
    return None

# compare the buy date
# and the evaluation date for one stock
def evaluate_pair(df, stock_code, buy_date, eval_date, max_gap_days, omega_tol, ri_tol):
    out = {"stock_code": stock_code, "buy_date": buy_date, "eval_date": eval_date, "score": None}
    hold = []

    # 매수일 행
    buy = find_row_on_or_before(df, stock_code, buy_date, max_gap_days)
    # 평가일 행
    now = find_row_on_or_before(df, stock_code, eval_date, max_gap_days)

    if buy is None:
        hold.append("buy_no_data")
    if now is None:
        hold.append("eval_no_data")
    if hold:
        out["hold_reason"] = "|".join(hold)
        return out

    # 값 기록
    out["omega_buy"] = buy["omega"]
    out["omega_eval"] = now["omega"]
    out["ri_buy"] = buy["residual_income"]
    out["ri_eval"] = now["residual_income"]

    # 제무재표 기준 다르면 유보
    if buy["fs_div_used"] != now["fs_div_used"]:
        hold.append("basis_changed")
    if not buy["omega_interpretable"]:
        hold.append("buy:" + buy["hold_reason"])
    if not now["omega_interpretable"]:
        hold.append("eval:" + now["hold_reason"])
    if hold:
        out["hold_reason"] = "|".join(hold)
        return out

    # 변화량: 평가일 값 - 매수일 값
    omega_dir = direction(now["omega"] - buy["omega"], omega_tol)
    ri_dir = direction(now["residual_income"] - buy["residual_income"], ri_tol)
    out["score"] = score_from_directions(omega_dir, ri_dir)

    if out["score"] is None:
        out["hold_reason"] = f"signal_conflict(omega_{omega_dir}, ri_{ri_dir})"
    else:
        out["hold_reason"] = ""
    return out




# main function for testing
if __name__ == "__main__":
    pd.set_option("display.float_format", "{:,.4f}".format)

    #자기자본비용(국채 3년물 금리 3.9%(현재 기준) + 주식 프리미엄(ERP) 4.6%(다모다란))
    r = 0.085
    #현재 0으로 유지
    vt = 0
    #ω 상한
    omega_max = 1 + r
    #직전 거래일 찾을 때 허용하는 최대 일수
    max_gap_days = 7

    omega_tol = 0
    ri_tol = 0


    hold_rules = [
        "flag_not_strict",
        "flag_capital_impairment",
        "flag_ri_negative",
        "flag_prem_negative",
        "flag_denominator_nonpositive",
        "flag_omega_out_of_range"
    ]


    raw = load_input(INPUT_PATH)
    result = add_ohlson_values(raw, r=r, vt=vt)
    result = add_flags(result, omega_max=omega_max)
    result = decide_interpretable(result, hold_rules=hold_rules)

    print("해석 가능 행: ", result["omega_interpretable"].sum())

    test_pairs = [
        ("036890", "2016-06-30", "2018-06-29"),   # 진성티이씨
        ("010170", "2018-09-07", "2019-01-09"),   # 대한광통신
        ("036890", "2016-06-30", "2019-06-28"),   # 진성티이씨
        ("032620", "2016-06-30", "2016-12-29"),   # GC메디아이
        ("010170", "2016-04-09", "2018-01-15"),   # 대한광통신
    ]
    
    for stock_code, buy_date, eval_date in test_pairs:
        res = evaluate_pair(result, stock_code, buy_date, eval_date, max_gap_days, omega_tol, ri_tol)
        print()
        print(stock_code, buy_date, "→", eval_date)
        print("  점수:", res["score"])
        print("  유보 사유:", res["hold_reason"])

