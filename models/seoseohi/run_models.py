"""
run_models.py

[what]
(종목, 매수일, 평가일) 쌍 목록을 넣으면 두 모델이 각자 위험점수를 계산하고 결과를 같은 모양의 표로 돌려준다.
오케스트라는 이 표만 읽으면 됌. fscore.py와 ohlson/ohlson.py는 고치지 않고 그대로 가져다 쓴다.

[how to use]
    from run_models import run_all, run_fscore, run_ohlson
    pairs = [("010170", "2016-06-30", "2019-06-28"), ...]
    table = run_all(pairs)         
    table = run_fscore(pairs)       # F-Score만
    table = run_ohlson(pairs)       # Ohlson만
    table = run_gate(pairs)         # 임계 모델 경보 (켜짐/꺼짐. run_all에는 포함되지 않는다)

[common column]
    model           "fscore", "ohlson" 또는 "dupont" (run_dupont는 외부 데이터 경로 환경변수가 필요)
    stock_code, buy_date, eval_date
    risk_score      위험점수. 높을수록 위험. 유보면 빈칸(<NA>)
    risk_max        이 모델의 최대 점수 (F-Score 3, Ohlson 2). 모델마다 눈금이 다르다
    interpretable   점수가 나왔으면 True, 유보면 False
    hold_reason     유보 사유 (점수가 나왔으면 빈 글자)
    scale_status    눈금이 팀 확정이면 "confirmed", 임시면 "provisional"
    + 모델별 근거 열 (F-Score: f_score_buy 등 / Ohlson: omega_state_buy 등)

[warning]
    - 점수 눈금이 다르다 (F-Score 0~3, Ohlson 0~2). 합치는 방법은 팀이 정함
    - 유보는 0점이 아니다. 합칠 때 "판단 안 함"으로 따로 다뤄야 한다
    - Ohlson 설정(r, vt 등)은 ohlson.py 실행부와 같아야 한다 → 직접 실행하면 점검한다

"""

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path

import pandas as pd


HERE = Path(__file__).resolve().parent                       # models/seoseohi
FSCORE_PATH = HERE / "fscore.py"
OHLSON_PATH = HERE / "ohlson" / "ohlson.py"                  # 폴더 안에 없으면 아래에서 HERE 바로 아래를 찾는다
if not OHLSON_PATH.exists():
    OHLSON_PATH = HERE / "ohlson.py"
THRESHOLD_PATH = HERE / "threshold.py"
OUTPUT_DIR = HERE / "output"

OHLSON_SETTINGS = {
    "r": 0.05,              # 자기자본비용
    "vt": 0,                # 비재무 정보. 0 고정
    "omega_high": 0.5,      # ω 상태 "높음" 기준
    "ri_small": 0.02,       # s 변화 2%p 미만은 "그대로"
    "ri_large": 0.05,       # s 변화 5%p 이상은 "많이 변함" (기록만)
    "max_gap_days": 7,      # 직전 거래일을 찾을 때 허용하는 최대 일수
}
OHLSON_HOLD_RULES = ["flag_not_strict", "flag_capital_impairment"]   # 유보 조건: 데이터 품질 문제만
OHLSON_RISK_MAX = 2
FSCORE_RISK_MAX = 3
DUPONT_RISK_MAX = 3

CORE_COLUMNS = ["model", "stock_code", "buy_date", "eval_date", "risk_score", "risk_max",
                "interpretable", "hold_reason", "scale_status"]
FSCORE_DETAIL = ["f_score_buy", "f_score_eval", "f_score_drop", "risk_drop", "risk_level_add",
                 "same_report", "fiscal_year_buy", "fiscal_year_eval", "rcept_no_buy", "rcept_no_eval"]
OHLSON_DETAIL = ["omega_state_buy", "omega_state_eval", "s_buy", "s_eval", "delta_s",
                 "ri_dir", "omega_dir", "ri_change_size"]
DUPONT_DETAIL = ["dPM_adj_pp", "dATO_rel_pct", "partial", "peer_fallback", "fs_div_flag",
                 "delisted", "delisted_in_window", "note"]
# 임계 모델(경보기)의 결과 열. 점수가 아니라서 risk_score, risk_max가 없다 (더하거나 합치지 않기 위해 일부러 뺐다)
GATE_COLUMNS = ["model", "stock_code", "buy_date", "eval_date", "alarm_on", "alarm_in_hold",
                "first_alarm_date", "last_alarm_date", "days_since_last_alarm", "n_onsets", "alarm_days",
                "first_alarm_day_share", "cusum_eval", "cum_ret_eval", "held_days",
                "k", "h", "baseline_mode", "cond3_mode", "interpretable", "hold_reason", "scale_status"]



def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=1)
def fscore_module():
    return load_module("fscore", FSCORE_PATH)


@lru_cache(maxsize=1)
def ohlson_module():
    return load_module("ohlson", OHLSON_PATH)


# 2
# F-Score: 회사 × 연도 점수 표
@lru_cache(maxsize=1)
def build_fscore_table():
    fs = fscore_module()
    data = fs.combine(fs.load_annual(fs.ANNUAL_FILE), fs.load_ltd(fs.LTD_FILE))
    data = fs.attach_f7(data, fs.load_f7(fs.F7_FILE))
    data = fs.add_prior_years(data)
    data = fs.apply_same_basis_prior(data, fs.load_same_basis(fs.SAME_BASIS_FILE))
    data = fs.add_signals(data)
    data = data[data["fiscal_year"] >= fs.FIRST_SCORE_YEAR].copy()
    return fs.add_fscore(data)


# Ohlson: 회사 × 거래일 계산 표 
@lru_cache(maxsize=1)
def build_ohlson_table():
    oh = ohlson_module()
    s = OHLSON_SETTINGS
    raw = oh.load_input(oh.INPUT_PATH)
    raw = raw[~raw["stock_code"].isin(oh.EXCLUDE_STOCKS)]
    result = oh.add_ohlson_values(raw, r=s["r"], vt=s["vt"])
    result = oh.add_flags(result, omega_max=1 + s["r"])
    result = oh.add_omega_state(result, omega_high=s["omega_high"])
    return oh.decide_interpretable(result, hold_rules=OHLSON_HOLD_RULES)



# 3. 공통 모양으로 바꾸기
def to_standard(model, table, risk_max, scale_status, detail_columns):
    out = pd.DataFrame({
        "model": model,
        "stock_code": table["stock_code"],
        "buy_date": table["buy_date"],
        "eval_date": table["eval_date"],
        "risk_score": table["risk_score"].astype("Int64"),
        "risk_max": risk_max,
        "hold_reason": table["hold_reason"].fillna(""),
        "scale_status": scale_status,
    })
    out["interpretable"] = out["risk_score"].notna()
    for col in detail_columns:                                  # 모델별 근거 열 (유보되면 없을 수 있음)
        out[col] = table[col] if col in table.columns else pd.NA
    return out[CORE_COLUMNS + detail_columns].reset_index(drop=True)


def run_fscore(pairs):
    fs = fscore_module()
    table = fs.evaluate_pairs(build_fscore_table(), pairs)
    status = "confirmed" if fs.RISK_SCALE_CONFIRMED else "provisional"
    return to_standard("fscore", table, FSCORE_RISK_MAX, status, FSCORE_DETAIL)


def run_ohlson(pairs):
    oh = ohlson_module()
    s = OHLSON_SETTINGS
    table = oh.evaluate_pairs(build_ohlson_table(), pairs, s["max_gap_days"], s["ri_small"], s["ri_large"])
    table["risk_score"] = table["score"]                        # Ohlson은 점수 열 이름이 score
    return to_standard("ohlson", table, OHLSON_RISK_MAX, "confirmed", OHLSON_DETAIL)


# DuPont: dupont.py의 dupont_compare를 그대로 쓴다 (dupont.py는 고치지 않음).
# 데이터 위치는 환경변수 SEOSEOHI_TTM_DIR, SEOSEOHI_FINAL_DATA_DIR (.env 가능)로 받는다. 코드에 경로를 박지 않는다.
def dupont_module():
    root = str(HERE.parents[1])                                 # dupont.py가 `models.seoseohi.config`를 import한다
    if root not in sys.path:
        sys.path.insert(0, root)
    import importlib
    return importlib.import_module("models.seoseohi.dupont")


@lru_cache(maxsize=1)
def build_dupont_data():
    import os
    dp = dupont_module()
    pl = importlib_pipeline()
    missing = [k for k in ("SEOSEOHI_TTM_DIR", "SEOSEOHI_FINAL_DATA_DIR") if not os.getenv(k)]
    if missing:
        dp.load_dotenv()                                        # .env에 있을 수 있
        missing = [k for k in missing if not os.getenv(k)]
    if missing:
        raise RuntimeError("DuPont 데이터 경로 환경변수가 없습니다: " + ", ".join(missing))
    return pl.load_data()


def importlib_pipeline():
    import importlib
    dupont_module()
    return importlib.import_module("models.seoseohi.pipeline")


def run_dupont(pairs):
    dp = dupont_module()
    data = build_dupont_data()
    rows = []
    for stock_code, buy_date, eval_date in pairs:
        r = dp.dupont_compare(data["ttm"], data["assets"], data["ksic"], data["stocks"], stock_code, buy_date, eval_date)
        pm, ato = r["pm_detail"], r["ato_detail"]
        score = r["score"]
        notes = [n for n in (pm.get("note"), ato.get("note")) if n]
        rows.append({
            "stock_code": stock_code, "buy_date": buy_date, "eval_date": eval_date,
            "risk_score": score,
            "hold_reason": "" if score is not None else "|".join(notes) or "no_score",
            "dPM_adj_pp": r["dPM_adj_pp"], "dATO_rel_pct": r["dATO_rel_pct"],
            # 한쪽 지표만으로 낸 점수는 partial로 표시한다 (점수는 dupont.py 규칙 그대로)
            "partial": score is not None and (r["dPM_adj_pp"] is None or r["dATO_rel_pct"] is None),
            "peer_fallback": "전체 표본 대체" in " ".join(str(pm.get(k, "")) for k in ("basis_buy", "basis_now")),
            "fs_div_flag": pm.get("fs_div_flag"),
            "delisted": r["delisted"], "delisted_in_window": r["delisted_in_window"],
            "note": "|".join(notes),
        })
    table = pd.DataFrame(rows)
    table["risk_score"] = table["risk_score"].astype("Int64")
    return to_standard("dupont", table, DUPONT_RISK_MAX, "provisional", DUPONT_DETAIL)


# 임계 모델: 매수일 이후 주가가 꾸준히 나빠졌는지 보는 경보기. threshold.py를 그대로 쓴다.
# 점수가 아니라 켜짐/꺼짐이라 run_all에 섞지 않는다. 본 데이터에서 경보는 이후의 큰 하락이나 펀더멘털 악화를 예고하지 않았다
# (link_fundamentals.py). "가격이 이미 약해졌다"는 상태 알림으로만 읽는다.
# 경보는 며칠만 켜졌다 꺼지는 일이 많아서 alarm_on(평가일 상태)보다 alarm_in_hold(보유 중 발생 여부)와
# last_alarm_date, days_since_last_alarm으로 읽는 편이 안전하다.
# 기본값은 팀 결정 그대로: k 0.5, h 5, 평소를 매일 새로 계산, 조건 3(하루 폭락 거르기) 끔.
@lru_cache(maxsize=1)
def threshold_module():
    return load_module("threshold", THRESHOLD_PATH)


def run_gate(pairs, k=None, h=None, baseline=None, cond3=None):
    tm = threshold_module()
    k = tm.DEFAULT_K if k is None else k
    h = tm.DEFAULT_H if h is None else h
    if not pairs:
        return pd.DataFrame(columns=GATE_COLUMNS)
    table = tm.run_threshold(pairs, k, h, baseline, cond3)
    out = pd.DataFrame({col: table[col] if col in table.columns else pd.NA for col in GATE_COLUMNS})
    for col in ("alarm_on", "alarm_in_hold", "interpretable"):          # 유보는 False가 아니라 빈칸이다
        out[col] = out[col].astype("boolean")
    for col in ("n_onsets", "alarm_days", "days_since_last_alarm", "held_days"):
        out[col] = out[col].astype("Int64")
    out["hold_reason"] = out["hold_reason"].fillna("")
    return out.reset_index(drop=True)


# 모델을 모두 돌려 한 표로 합친다 (쌍마다 모델 수만큼 줄)
def run_all(pairs, include_dupont=False):
    parts = [run_fscore(pairs), run_ohlson(pairs)]
    if include_dupont:                                          # DuPont은 외부 데이터 경로가 필요해 선택 사항
        parts.append(run_dupont(pairs))
    both = pd.concat(parts, ignore_index=True)
    return both.sort_values(["stock_code", "buy_date", "eval_date", "model"]).reset_index(drop=True)



# 4. 직접 실행: 
# (쌍, 기대 위험점수, 기대 유보 사유). 점수가 있으면 유보 사유는 빈 글자
FSCORE_CHECK = [
    (("010170", "2016-06-30", "2019-06-28"), 1, ""),                  # 7 → 3, 하락 4 → 1점
    (("036890", "2016-06-30", "2019-06-28"), 1, ""),                  # 7 → 4, 하락 3 → 1점
    (("010170", "2017-06-30", "2017-12-28"), None, "same_report"),    # 같은 보고서 → 유보
    (("052270", "2016-06-30", "2019-06-28"), None, "after_delisting"),
    (("010170", "2015-06-30", "2016-06-30"), None, "buy_no_data"),
]
OHLSON_CHECK = [
    (("036890", "2016-06-30", "2018-06-29"), 0, ""),
    (("010170", "2016-03-31", "2016-12-21"), 1, ""),
    (("036890", "2019-06-10", "2019-12-02"), 1, ""),
    (("010170", "2017-12-19", "2019-09-06"), 2, ""),
    (("032620", "2016-06-30", "2016-12-29"), None, "basis_changed"),  # 연결 → 별도 → 유보
    (("010170", "2016-04-09", "2018-01-15"), 0, ""),
]


# DuPont은 외부 데이터 경로(환경변수)가 있을 때만 점검한다. 현진소재 기대값은 pipeline.py 주석(점수 3, 상장폐지)과 같다
DUPONT_CHECK = [
    (("053660", "2017-06-30", "2019-12-30"), 3, ""),
    (("083650", "2017-06-30", "2019-12-30"), 3, ""),
    (("036890", "2017-06-30", "2019-12-30"), 0, ""),
    (("050540", "2017-06-30", "2019-12-30"), None, "결측"),
]


# 임계 모델 점검: (쌍, 첫 경보일, 켜진 날 수, 유보 사유). k 0.5, h 5, 평소 이동, 조건 3 끔 (팀 결정 그대로)
GATE_CHECK = [
    (("053660", "2016-06-30", "2019-06-28"), "2018-12-19", 4, ""),                  # 현진소재: 하루 -30% 급락이 만든 경보
    (("010170", "2016-06-30", "2019-06-28"), "2018-10-29", 2, ""),                  # 대한광통신
    (("036890", "2016-06-30", "2019-06-28"), None, 0, ""),                          # 진성티이씨: 경보 없음
    (("052270", "2016-06-30", "2019-06-28"), None, None, "eval_no_data"),           # 상장폐지 이후
    (("010170", "2017-06-30", "2016-06-30"), None, None, "eval_not_after_buy"),
    (("010170", "2015-03-02", "2015-06-30"), None, None, "no_baseline_yet"),        # 150거래일 자료가 안 쌓임
]


def check_gate(cases):
    gate = run_gate([pair for pair, *_ in cases])
    ok_all = True
    print("[임계 모델(gate)] k 0.5, h 5, 평소 이동, 조건 3 끔 (점수가 아니라 경보)")
    for (pair, want_first, want_days, want_hold), (_, row) in zip(cases, gate.iterrows()):
        first = None if pd.isna(row["first_alarm_date"]) else row["first_alarm_date"].strftime("%Y-%m-%d")
        days = None if pd.isna(row["alarm_days"]) else int(row["alarm_days"])
        ok = first == want_first and days == want_days and want_hold in row["hold_reason"]
        ok_all &= ok
        mark = "OK  " if ok else "다름"
        print(f"  {mark} {pair[0]} {pair[1]}→{pair[2]}  첫 경보 {first} (기대 {want_first})  켜진 날 {days} (기대 {want_days})  유보 '{row['hold_reason']}'")
    return ok_all


def check_model(name, run, cases):
    result = run([pair for pair, _, _ in cases])
    ok_all = True
    print(f"[{name}]")
    for (pair, want_score, want_hold), (_, row) in zip(cases, result.iterrows()):
        got = None if pd.isna(row["risk_score"]) else int(row["risk_score"])
        ok = (got == want_score) and (want_hold in row["hold_reason"])
        ok_all &= ok
        mark = "OK  " if ok else "다름"
        print(f"  {mark} {pair[0]} {pair[1]}→{pair[2]}  점수 {got} (기대 {want_score})  유보 '{row['hold_reason']}'")
    return ok_all


if __name__ == "__main__":
    ok_f = check_model("F-Score", run_fscore, FSCORE_CHECK)
    ok_o = check_model("Ohlson", run_ohlson, OHLSON_CHECK)
    import os
    if os.getenv("SEOSEOHI_TTM_DIR") and os.getenv("SEOSEOHI_FINAL_DATA_DIR"):
        ok_o &= check_model("DuPont", run_dupont, DUPONT_CHECK)
    else:
        print("[DuPont] 환경변수 SEOSEOHI_TTM_DIR / SEOSEOHI_FINAL_DATA_DIR가 없어 건너뜀")
    ok_g = check_gate(GATE_CHECK)
    print()
    print("F-Score 눈금 상태:", run_fscore([FSCORE_CHECK[0][0]]).loc[0, "scale_status"])
    both = run_all([FSCORE_CHECK[0][0], OHLSON_CHECK[1][0]])
    OUTPUT_DIR.mkdir(exist_ok=True)
    both.to_csv(OUTPUT_DIR / "risk_models_check.csv", index=False, encoding="utf-8-sig")
    print("공통 표 열:", list(both.columns[:9]))
    print("점검 결과:", "모두 기대와 같음" if ok_f and ok_o and ok_g else "다른 항목 있음 → 위 '다름' 줄 확인")
