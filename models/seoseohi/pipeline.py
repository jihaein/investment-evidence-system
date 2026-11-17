import os
import pandas as pd
from dotenv import load_dotenv
from .dupont import (load_ttm, load_ksic, load_assets, load_stocks,
                     filter_to_universe, dupont_compare,
                     dupont_compare_pit, load_ksic_from_base)
from .fscore import build_table as build_fscore_table, fscore_compare, fscore_compare_pit
from .ohlson.ohlson import build_table as build_ohlson_table, ohlson_compare, ohlson_compare_pit


def load_data() -> dict:
    load_dotenv()
    ttm = load_ttm(); ksic = load_ksic(); stocks = load_stocks()
    assets = load_assets(os.path.join(os.getenv("SEOSEOHI_FINAL_DATA_DIR"),
                                      "03_재무_분기_2015-2019.csv"))
    ttm = filter_to_universe(ttm, ksic)
    return {
        "ttm": ttm, "ksic": ksic, "assets": assets, "stocks": stocks,
        "fscore_table": build_fscore_table(),
        "ohlson_table": build_ohlson_table(),
    }

def run_seoseohi(data, portfolio, now_date) -> dict:
    s, buy = portfolio["stock_code"], portfolio["buy_date"]
    dupont = dupont_compare(data["ttm"], data["assets"], data["ksic"],
                            data["stocks"], s, buy, now_date)
    fscore = fscore_compare(data["fscore_table"], s, buy, now_date)
    ohlson = ohlson_compare(data["ohlson_table"], s, buy, now_date)
    scores = {"dupont": dupont.get("score"),
            "fscore": fscore.get("risk_score"),
            "ohlson": ohlson.get("score")}
    return {
        "portfolio": portfolio, "now_date": now_date,
        "verdict": integrate(scores),
        "dupont": {"score": scores["dupont"], "detail": dupont},
        "fscore": {"score": scores["fscore"], "detail": fscore},
        "ohlson": {"score": scores["ohlson"], "detail": ohlson},
    }


# ============================================================
#  스냅샷(점별) 모드 — 백테스트 120종목용, 미래정보 차단판
# ------------------------------------------------------------
#  세 모형 모두 기준일별 스냅샷을 읽는 _pit 함수로 바꾼 버전.
#  무거운 표를 미리 만들지 않고, 매수일·평가일마다 알맞은 스냅샷을 그때그때 읽는다
#  (스냅샷 표는 각 모듈이 캐시하므로 반복 호출해도 한 번만 만든다).
#  load_data_pit 은 기준일과 무관한 KSIC(업종)만 미리 읽어 두면 된다.
# ============================================================

def load_data_pit(base_dir=None) -> dict:
    """스냅샷 모드 입력. base_dir 이 없으면 .env 의 BACKTEST_DATA_DIR 를 쓴다."""
    load_dotenv()
    base_dir = base_dir or os.getenv("BACKTEST_DATA_DIR")
    return {"base_dir": base_dir, "ksic": load_ksic_from_base(base_dir)}


def run_seoseohi_pit(data, portfolio, now_date) -> dict:
    """스냅샷 기반 서서히 판정. run_seoseohi 와 결과 구조는 같고, 안에서 _pit 함수를 쓴다."""
    base = data["base_dir"]
    ksic = data["ksic"]
    s, buy = portfolio["stock_code"], portfolio["buy_date"]

    dupont = dupont_compare_pit(base, ksic, s, buy, now_date)
    fscore = fscore_compare_pit(base, s, buy, now_date)
    ohlson = ohlson_compare_pit(base, s, buy, now_date)
    scores = {"dupont": dupont.get("score"),
              "fscore": fscore.get("risk_score"),
              "ohlson": ohlson.get("score")}
    return {
        "portfolio": portfolio, "now_date": now_date,
        "verdict": integrate(scores),
        "scores": scores,
        "dupont": {"score": scores["dupont"], "detail": dupont},
        "fscore": {"score": scores["fscore"], "detail": fscore},
        "ohlson": {"score": scores["ohlson"], "detail": ohlson},
    }


# 각 모형 점수가 이 값 이상이면 "악화 모형"으로 센다 (스케일 반영, 초기 가설 — 팀 조정 대상)
BAD_THRESHOLD = {"dupont": 2, "fscore": 2, "ohlson": 2}   # ohlson은 2가 최대치 = 동반 악화
MIN_VALID = 2   # 계산된 모형이 이보다 적으면 유보

def integrate(scores: dict) -> str:
    # scores = {"dupont": 3, "fscore": 2, "ohlson": None, ...}
    # 결측은 None 뿐 아니라 pandas NA/NaN 으로도 올 수 있어 pd.isna 로 함께 거른다.
    # 유효값은 plain int 로 맞춰 Int64/np.int64 비교에서 생기는 문제를 없앤다.
    valid = {k: int(v) for k, v in scores.items() if not pd.isna(v)}
    if len(valid) < MIN_VALID:
        return "유보"
    bad = sum(1 for k, v in valid.items() if v >= BAD_THRESHOLD[k])
    if bad >= 2:
        return "Yes(훼손 우려)"
    if bad == 1:
        return "주의"
    return "No"


# 합산 판정 (PM 2026-11-13 추가): 세 모델 점수를 더해 SUM_THRESHOLD 이상이면 매도.
# 개수 방식을 포함하는 상위 규칙 — 점수 단계(1점/3점 등)가 판정에 반영된다.
SUM_THRESHOLD = 4

def integrate_sum(scores: dict) -> str:
    valid = {k: int(v) for k, v in scores.items() if not pd.isna(v)}
    if len(valid) < MIN_VALID:
        return "유보"
    if sum(valid.values()) >= SUM_THRESHOLD:
        return "Yes(훼손 우려)"
    if any(v >= BAD_THRESHOLD[k] for k, v in valid.items()):
        return "주의"
    return "No"


if __name__ == "__main__":
    data = load_data()
    # 현진소재 (3,2,2) → 세 모형 모두 악화 → Yes
    # 진성티이씨 (0,0,0) → 악화 0개 → No
    print(run_seoseohi(data, {"stock_code":"053660","buy_date":"2017-06-30"}, "2019-12-30")["verdict"])  # Yes(훼손 우려)
    print(run_seoseohi(data, {"stock_code":"036890","buy_date":"2016-06-30"}, "2018-06-29")["verdict"])  # No


