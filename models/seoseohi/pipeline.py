import os
from dotenv import load_dotenv
from .dupont import (load_ttm, load_ksic, load_assets, load_stocks,
                     filter_to_universe, dupont_compare)
from .fscore import build_table as build_fscore_table, fscore_compare
from .ohlson.ohlson import build_table as build_ohlson_table, ohlson_compare


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


# 각 모형 점수가 이 값 이상이면 "악화 모형"으로 센다 (스케일 반영, 초기 가설 — 팀 조정 대상)
BAD_THRESHOLD = {"dupont": 2, "fscore": 2, "ohlson": 2}   # ohlson은 2가 최대치 = 동반 악화
MIN_VALID = 2   # 계산된 모형이 이보다 적으면 유보

def integrate(scores: dict) -> str:
    # scores = {"dupont": 3, "fscore": 2, "ohlson": None, ...}
    valid = {k: v for k, v in scores.items() if v is not None}
    if len(valid) < MIN_VALID:
        return "유보"
    bad = sum(1 for k, v in valid.items() if v >= BAD_THRESHOLD[k])
    if bad >= 2:
        return "Yes(훼손 우려)"
    if bad == 1:
        return "주의"
    return "No"


if __name__ == "__main__":
    data = load_data()
    # 현진소재 (3,2,2) → 세 모형 모두 악화 → Yes
    # 진성티이씨 (0,0,0) → 악화 0개 → No
    print(run_seoseohi(data, {"stock_code":"053660","buy_date":"2017-06-30"}, "2019-12-30")["verdict"])  # Yes(훼손 우려)
    print(run_seoseohi(data, {"stock_code":"036890","buy_date":"2016-06-30"}, "2018-06-29")["verdict"])  # No


