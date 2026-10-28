"""
ohlson.py — 서서히 모델 중 수정 Ohlson 모델

[무엇을 하나]
매수일과 평가일의 잔여이익(RI)과 이익 지속성(ω)을 비교해서
"샀을 때보다 회사가 나빠졌는가"를 0~2점으로 판정한다.

[계산식]
    RI   = NI - r * BV(전기)
    Prem = 시가총액 - BV(최근) - vt
    ω    = (1 + r) * Prem / (Prem + RI)
    s    = RI / BV(전기) ← 회사 크기와 상관없이 RI를 비교하기 위한 비율
    ω와 RI는 곱하지 않고 따로 본다.

[입력]
ohlson_input_final.csv: 종목 * 거래일. 재무값은 분기마다, 시가총액은 매일 바뀐다.

[팀 결정 사항] (2026-10-26)
    r = 5%            국채 3.9%는 2026년 금리라 분석 기간(2016~2019)과 맞지 않아 조정
    NI = 지배주주 순이익 TTM (최근 4분기 합)
    ω 상태            높음(ω ≥ 0.5) / 낮음(0 ≤ ω < 0.5) / 최하(적자 등 계산 불가)
    RI 방향           s 변화가 2%p 미만이면 "그대로"
    점수              RI 기준 3*3 표 (SCORE_TABLE)
    변화 크기         2~5%p "조금", 5%p 이상 "많이" → 아직 기록만, 점수에는 미반영
    유보              데이터 품질 문제(strict 아님), 자본잠식, 재무제표 기준 변경, 데이터 없음

[실행 순서]
    load_input → add_ohlson_values → add_flags → add_omega_state
    → decide_interpretable → evaluate_pairs → CSV 저장
"""


"""
[남은 일] (2026-10-26 기준)
1. 본 데이터로 테스트
   - 본 데이터에 분기별 지배주주 값(순이익 TTM, 자본, 4분기 전 자본)이 없음
   - 데이터 담당에게 요청 필요. 받으면 입력표만 바꾸고 이 파일은 그대로
   - 그 전까지는 기존 ohlson_input_final(다날 제외, 분기 갱신)로 분석
   - build_ohlson_input.py(04 + 05, 연간 갱신)는 사업보고서 공시일 값 대조용
2. 성능 검증: 백테스트와 2020년 이후 사후 검증 (기준값 r, ω 0.5, s 2%p 조정)
3. 팀 재검토: ω 0.5 기준(low가 268행뿐), 변화 크기(조금/많이)를 점수에 넣을지
4. F-Score, DuPont과 합치기
"""


from pathlib import Path
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
INPUT_PATH = PROJECT_ROOT / "data" / "ohlson" / "ohlson_input_daily.csv"
OUTPUT_DIR = Path(__file__).parent / "output"

TEXT_COLUMNS = ["stock_code", "corp_code", "ni_receipt", "bv_t_receipt", "bv_prev_receipt"]
NUMBER_COLUMNS = ["ni_ttm_used", "bv_t", "bv_prev", "mktcap", "list_shrs"]

# 분석에서 제외할 종목
EXCLUDE_STOCKS = ["064260"]  # 다날: 매출을 영업수익으로 표시해 본 데이터에서 제외 (PM 결정)

# ω 상태의 순서. 숫자가 클수록 좋은 상태 → 두 날짜의 숫자 차이로 방향을 판정
STATE_RANK = {"lowest": 0, "low": 1, "high": 2}

# (RI 방향, ω 방향) → 점수
# RI(실제 실적)를 주축, ω(주가에서 역산한 시장 기대)를 보조축으로 본다.
#   - RI 상승 줄은 모두 0점: RI가 오르면 식 구조상 ω가 떨어지기 쉬워서, ω 하락만으로 악화로 보지 않음
#   - RI 그대로 + ω 하락 = 1점: 실적은 유지, 시장 기대만 나빠짐
#   - RI 하락 + ω 상승/그대로 = 1점: 실적은 나빠졌지만 시장은 아직 일시적이라고 봄
#   - RI 하락 + ω 하락 = 2점: 실적과 시장 기대가 함께 나빠짐
SCORE_TABLE = {
    ("up", "up"): 0,   ("up", "flat"): 0,   ("up", "down"): 0,
    ("flat", "up"): 0, ("flat", "flat"): 0, ("flat", "down"): 1,
    ("down", "up"): 1, ("down", "flat"): 1, ("down", "down"): 2,
}


# step 1. read data
def load_input(path):
    df = pd.read_csv(path, dtype={col: str for col in TEXT_COLUMNS}, encoding="utf-8-sig")
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for col in NUMBER_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["input_ready_strict"] = df["input_ready_strict"].astype(str).str.lower().eq("true")
    return df


# strict 행만
def filter_strict(df):
    return df[df["input_ready_strict"]].copy()


# step 2. calculate RI, prem, ω, s
def add_ohlson_values(df, r, vt):
    out = df.copy()
    out["r_used"] = r  # 이번 계산에 쓴 r을 기록

    # RI (잔여이익) = NI - r × BV(전기)
    out["residual_income"] = out["ni_ttm_used"] - r * out["bv_prev"]

    # Prem (시장 프리미엄) = 시가총액 - BV(최근) - vt
    out["market_premium"] = out["mktcap"] - out["bv_t"] - vt

    # ω의 분모. 3단계에서 0 이하인지 다시 확인한다
    denominator = out["market_premium"] + out["residual_income"]

    # ω (이익 지속성) = (1 + r) × Prem / (Prem + RI)
    out["omega"] = (1 + r) * out["market_premium"] / denominator

    # s (RI Spread) = RI / BV(전기)
    # RI를 금액 대신 자본 대비 비율로 바꿔 회사 크기와 상관없이 비교한다.
    # RI 변화율을 나눗셈으로 구하면 RI가 음수일 때 부호가 뒤집혀서 s의 차이로 본다.
    out["ri_spread"] = out["residual_income"] / out["bv_prev"]
    return out


# step 3. 조건 표시와 ω 상태 분류
# [발견] RI와 Prem이 둘 다 음수면 ω가 그럴듯해 보여도 방향이 거꾸로 움직인다.
#   대한광통신 2016-04-05 → 04-06: 주가 상승으로 Prem이 -133억 → -91억으로 개선됐는데
#   ω는 0.69 → 0.59로 하락 (음수끼리 나눠서 생기는 현상)
# [발견] RI < 0이고 Prem > 0이면 ω는 수학적으로 항상 1 + r을 넘는다.
# → 적자이거나 계산이 불안정한 행은 ω 숫자를 쓰지 않고 "최하 상태"로 분류한다.

# 조건별 해당 여부를 True/False로 표시
def add_flags(df, omega_max):
    out = df.copy()
    denominator = out["market_premium"] + out["residual_income"]

    # 자본잠식: 자본이 0 이하라 Prem 자체가 비정상 → 유보
    out["flag_capital_impairment"] = out["capital_impairment"].eq("Y")

    # 적자: RI < 0. 초과이익이 없으니 ω(지속성)를 물을 수 없음 → 최하 상태
    out["flag_ri_negative"] = out["residual_income"] < 0

    # 시총 < 자본: Prem < 0이면 ω 방향이 뒤집힐 수 있음 → 최하 상태
    out["flag_prem_negative"] = out["market_premium"] < 0

    # ω 분모가 0 이하: 나눗셈 결과가 비정상 → 최하 상태
    out["flag_denominator_nonpositive"] = denominator <= 0

    # ω가 수학적 범위(0 ~ 1 + r)를 벗어남. 현재는 참고용으로만 기록
    out["flag_omega_out_of_range"] = (out["omega"] < 0) | (out["omega"] >= omega_max)

    # strict 아님: 근거 없는 대체값이 섞였거나 재료가 빠짐 → 유보
    out["flag_not_strict"] = ~out["input_ready_strict"]
    return out


# ω를 높음 / 낮음 / 최하 세 상태로 분류
def add_omega_state(df, omega_high):
    out = df.copy()
    lowest = (
        out["flag_ri_negative"]
        | out["flag_prem_negative"]
        | out["flag_denominator_nonpositive"]
        | (out["omega"] < 0)
    )
    out["omega_state"] = "low"                                    # 1. 일단 모두 "낮음"
    out.loc[out["omega"] >= omega_high, "omega_state"] = "high"   # 2. 기준 이상이면 "높음"
    out.loc[lowest, "omega_state"] = "lowest"                     # 3. 마지막에 최하로 덮어씀
    # 3번을 마지막에 하는 이유: RI·Prem이 둘 다 음수면 ω가 0.67처럼 나와
    # 2번에서 "높음"이 될 수 있다. 그런 행을 확실히 최하로 보내기 위해서다.
    return out


# step 4. 고른 유보 조건으로 이 날짜를 비교에 쓸 수 있는가 판정
# omega_interpretable: True면 사용 가능. (적자 등은 이제 유보가 아니라 최하 상태로 점수에 들어감)
def decide_interpretable(df, hold_rules):
    out = df.copy()
    # 고른 조건 중 하나라도 해당하면 사용 불가
    out["omega_interpretable"] = ~out[hold_rules].any(axis=1)

    # 걸린 조건 이름을 "not_strict|capital_impairment"처럼 이어 붙여 사유로 남긴다
    out["hold_reason"] = ""
    for col in hold_rules:
        name = col.replace("flag_", "")
        mask = out[col]
        out.loc[mask, "hold_reason"] = out.loc[mask, "hold_reason"] + name + "|"
    out["hold_reason"] = out["hold_reason"].str.rstrip("|")
    return out


# step 5. 날짜로 행 찾기
# strict 행만 남긴 표가 아니라 전체 표에서 찾는다.
# strict 표에서 찾으면 빠진 날짜를 며칠 전 행으로 조용히 바꿔치기하기 때문이다.

# 그 날짜의 행을 찾는다. 휴장일이면 직전 거래일 행을 쓴다.
def find_row_on_or_before(df, stock_code, date, max_gap_days):
    date = pd.Timestamp(date)
    rows = df[(df["stock_code"] == stock_code) & (df["trade_date"] <= date)]
    if rows.empty:
        return None  # 데이터 시작 전 날짜
    row = rows.sort_values("trade_date").iloc[-1]  # 그 날짜 이전 중 가장 최근 행
    if (date - row["trade_date"]).days > max_gap_days:
        return None  # 너무 오래된 값은 쓰지 않음 (거래정지 등)
    return row


# step 6. 매수일과 평가일 비교

# 변화량을 상승 / 하락 / 그대로로 분류. tolerance 이내면 그대로
def direction(change, tolerance):
    if change > tolerance:
        return "up"
    if change < -tolerance:
        return "down"
    return "flat"


# ω 상태가 올라갔는지 내려갔는지 판정 (예: high(2) → lowest(0)이면 down)
def state_direction(buy_state, eval_state):
    diff = STATE_RANK[eval_state] - STATE_RANK[buy_state]
    if diff > 0:
        return "up"
    if diff < 0:
        return "down"
    return "flat"


# 변화 크기를 없음 / 조금 / 많이로 분류 (팀 결정 ④). 점수에는 아직 쓰지 않고 기록만
def change_size(change, small, large):
    size = abs(change)
    if size < small:
        return "none"
    if size < large:
        return "small"
    return "large"


# 종목 하나의 매수일과 평가일을 비교해 점수 또는 유보 사유를 돌려준다
def evaluate_pair(df, stock_code, buy_date, eval_date, max_gap_days, ri_small, ri_large):
    out = {"stock_code": stock_code, "buy_date": buy_date, "eval_date": eval_date, "score": None}
    hold = []

    # 1) 두 날짜의 행 찾기. 없으면 여기서 끝
    buy = find_row_on_or_before(df, stock_code, buy_date, max_gap_days)
    now = find_row_on_or_before(df, stock_code, eval_date, max_gap_days)
    if buy is None:
        hold.append("buy_no_data")
    if now is None:
        hold.append("eval_no_data")
    if hold:
        out["hold_reason"] = "|".join(hold)
        return out

    # 2) 유보되더라도 두 날의 값은 남겨둔다 (나중에 왜 유보됐는지 볼 수 있게)
    out["omega_buy"] = buy["omega"]
    out["omega_eval"] = now["omega"]
    out["omega_state_buy"] = buy["omega_state"]
    out["omega_state_eval"] = now["omega_state"]
    out["s_buy"] = buy["ri_spread"]
    out["s_eval"] = now["ri_spread"]

    # 3) 비교해도 되는지 확인. 하나라도 걸리면 여기서 끝
    if buy["fs_div_used"] != now["fs_div_used"]:
        hold.append("basis_changed")  # 연결↔별도: 같은 잣대로 비교 불가
    if not buy["omega_interpretable"]:
        hold.append("buy:" + buy["hold_reason"])
    if not now["omega_interpretable"]:
        hold.append("eval:" + now["hold_reason"])
    if hold:
        out["hold_reason"] = "|".join(hold)
        return out

    # 4) 방향 판정
    delta_s = now["ri_spread"] - buy["ri_spread"]                          # s 변화 (%p)
    ri_dir = direction(delta_s, ri_small)                                  # RI 방향
    omega_dir = state_direction(buy["omega_state"], now["omega_state"])    # ω 방향

    # 5) 점수: 3×3 표에서 꺼낸다. 9칸이 모두 채워져 있어 신호 충돌 유보는 없음
    out["delta_s"] = delta_s
    out["ri_dir"] = ri_dir
    out["omega_dir"] = omega_dir
    out["ri_change_size"] = change_size(delta_s, ri_small, ri_large)
    out["score"] = SCORE_TABLE[(ri_dir, omega_dir)]
    out["hold_reason"] = ""
    return out


# 여러 (종목, 매수일, 평가일) 쌍을 평가해 표로 만든다
def evaluate_pairs(df, pairs, max_gap_days, ri_small, ri_large):
    rows = []
    for stock_code, buy_date, eval_date in pairs:
        rows.append(evaluate_pair(df, stock_code, buy_date, eval_date, max_gap_days, ri_small, ri_large))
    return pd.DataFrame(rows)


def build_table(r=0.05, vt=0, omega_high=0.5, hold_rules=None):
    hold_rules = hold_rules or ["flag_not_strict", "flag_capital_impairment"]
    raw = load_input(INPUT_PATH)
    raw = raw[~raw["stock_code"].isin(EXCLUDE_STOCKS)]
    result = add_ohlson_values(raw, r=r, vt=vt)
    result = add_flags(result, omega_max=1 + r)
    result = add_omega_state(result, omega_high=omega_high)
    result = decide_interpretable(result, hold_rules=hold_rules)
    return result

def ohlson_compare(table, stock_code, buy_date, now_date,
                   max_gap_days=7, ri_small=0.02, ri_large=0.05):
    return evaluate_pair(table, stock_code, buy_date, now_date,
                         max_gap_days, ri_small, ri_large)


"""
# 실행 

if __name__ == "__main__":
    pd.set_option("display.float_format", "{:,.4f}".format)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", None)
    pd.set_option("display.max_colwidth", None)

    # ---- 모델 설정 -----
    r = 0.05            # 자기자본비용. 분석 기간(2016~2019) 금리 수준을 고려해 5%
    vt = 0              # 비재무 정보. 0 고정 (vt 실험은 별도)
    omega_max = 1 + r   # ω 수학적 상한. 참고 플래그에만 사용
    omega_high = 0.5    # ω 상태 "높음" 기준
    ri_small = 0.02     # s 변화 2%p 미만은 "그대로"
    ri_large = 0.05     # s 변화 5%p 이상은 "많이 변함" (기록만)
    max_gap_days = 7    # 직전 거래일을 찾을 때 허용하는 최대 일수

    # 유보 조건: 데이터 품질 문제만. 적자 등은 최하 상태로 점수에 반영
    hold_rules = [
        "flag_not_strict",
        "flag_capital_impairment",
    ]

    # ---- 계산 ----
    raw = load_input(INPUT_PATH)
    raw = raw[~raw["stock_code"].isin(EXCLUDE_STOCKS)]   # 제외 종목 빼기

    result = add_ohlson_values(raw, r=r, vt=vt)
    result = add_flags(result, omega_max=omega_max)
    result = add_omega_state(result, omega_high=omega_high)
    result = decide_interpretable(result, hold_rules=hold_rules)

    print("종목 수:", result["stock_code"].nunique())
    print("사용 가능 행:", result["omega_interpretable"].sum())
    print(result.loc[result["omega_interpretable"], "omega_state"].value_counts())

    # ---- 테스트 쌍: 0점, 1점(두 경우), 2점, 유보가 모두 나오도록 고름 ----
    test_pairs = [
        ("036890", "2016-06-30", "2018-06-29"),   # 진성티이씨: 실적·기대 유지 → 0점
        ("010170", "2016-03-31", "2016-12-21"),   # 대한광통신: 적자 지속 + 악화 → 1점
        ("036890", "2019-06-10", "2019-12-02"),   # 진성티이씨: 실적 유지, 기대 하락 → 1점
        ("010170", "2017-12-19", "2019-09-06"),   # 대한광통신: 흑자 → 적자 → 2점
        ("032620", "2016-06-30", "2016-12-29"),   # GC메디아이: 연결 → 별도 → 유보
        ("010170", "2016-04-09", "2018-01-15"),   # 대한광통신: 적자 → 흑자 → 0점
    ]
    eval_table = evaluate_pairs(result, test_pairs, max_gap_days, ri_small, ri_large)
    eval_table["score"] = eval_table["score"].astype("Int64")   # 빈칸을 허용하는 정수형

    # 이번 실행에 쓴 설정을 결과에 기록 (파일만 보고도 어떤 설정인지 알 수 있게)
    eval_table["r_used"] = r
    eval_table["omega_high"] = omega_high
    eval_table["ri_small"] = ri_small
    eval_table["ri_large"] = ri_large

    print(eval_table[[
        "stock_code", "buy_date", "eval_date",
        "omega_state_buy", "omega_state_eval", "s_buy", "s_eval",
        "ri_dir", "omega_dir", "ri_change_size", "score", "hold_reason",
    ]])

    # ---- 저장 ----
    daily_columns = [
        "trade_date", "stock_code", "company_name", "fs_div_used",
        "ni_ttm_used", "bv_t", "bv_prev", "mktcap",
        "r_used", "residual_income", "market_premium", "omega", "ri_spread",
        "omega_state", "omega_interpretable", "hold_reason", "capital_impairment",
    ]
    OUTPUT_DIR.mkdir(exist_ok=True)
    result[daily_columns].to_csv(OUTPUT_DIR / "ohlson_daily.csv", index=False, encoding="utf-8-sig")
    eval_table.to_csv(OUTPUT_DIR / "ohlson_eval.csv", index=False, encoding="utf-8-sig")
    print("저장 완료:", OUTPUT_DIR)

"""