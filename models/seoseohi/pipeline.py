import os
from dotenv import load_dotenv
from .dupont import (load_ttm, load_ksic, load_assets, load_stocks, filter_to_universe, dupont_compare)

def load_data() -> dict:
    load_dotenv()
    ttm = load_ttm()
    ksic = load_ksic()
    stocks = load_stocks()
    assets = load_assets(os.path.join(os.getenv("SEOSEOHI_FINAL_DATA_DIR"),
                                      "03_재무_분기_2015-2019.csv"))
    ttm = filter_to_universe(ttm, ksic)     # 19 universe
    return {"ttm": ttm, "ksic": ksic, "assets": assets, "stocks": stocks}

def run_seoseohi(data, portfolio, now_date) -> dict:
    s   = portfolio["stock_code"]
    buy = portfolio["buy_date"]
    dupont = dupont_compare(data["ttm"], data["assets"], data["ksic"],
                            data["stocks"], s, buy, now_date)
    return {
        "portfolio": portfolio,
        "now_date": now_date,
        "dupont": dupont,
        "ohlson": None,   # Phase 3
        "fscore": None,   # 타팀
    }

if __name__ == "__main__":
    data = load_data()
    pf = {"stock_code": "053660", "buy_date": "2017-06-30", "avg_cost": None, "recent_price": None}
    result = run_seoseohi(data, pf, "2019-12-30")
    print(result["dupont"]["score"], result["dupont"]["delisted"])   # → 3 True