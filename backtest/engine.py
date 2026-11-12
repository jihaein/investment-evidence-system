import os
from dotenv import load_dotenv
import pandas as pd

INITIAL_CASH = 10_000_000
COST_RATE = 0.001  # 0.1% transaction fee
CASH_DAILY_RATE = 0.000034


def load_daily_price() -> pd.DataFrame:
    """Daily % return grid (date × stock_code) for the 120-stock backtest.

    Source: BACKTEST_DATA_DIR/추가자료/시장자료/120종목_일별시가총액_2015-11-30_2020-12-31.csv

    등락률(FLUC_RT) 컬럼이 있으면 그것을 그대로 쓴다. FLUC_RT 는 거래소 공식 일간수익률로,
    감자·액면분할·유상증자 같은 기업행위를 이미 반영한다(= 미조정 종가로 계산할 때 생기는
    가짜 급등락이 없다). 예: 073570 2017-11-15 종가는 944→5,090 으로 뛰지만 FLUC_RT 는 +1.6%.

    (구버전 호환) FLUC_RT 가 없으면 미조정 종가(TDD_CLSPRC)의 pct_change 로 대체 계산하되,
    이 경우 기업행위가 가짜 수익률로 잡힐 수 있다.
    """
    load_dotenv()
    base = os.getenv("BACKTEST_DATA_DIR")
    file_path = os.path.join(
        base, "추가자료", "시장자료",
        "120종목_일별시가총액_2015-11-30_2020-12-31.csv",
    )

    data = pd.read_csv(file_path, header=0, dtype=str, encoding="utf-8-sig")
    data["stock_code"] = data["stock_code"].str.zfill(6)
    data["BAS_DD"] = pd.to_datetime(data["BAS_DD"], format="%Y%m%d", errors="coerce")

    if "FLUC_RT" in data.columns:
        data["FLUC_RT"] = pd.to_numeric(data["FLUC_RT"], errors="coerce")
        return data.pivot(index="BAS_DD", columns="stock_code", values="FLUC_RT")

    # fallback: 등락률이 없는 구버전 파일 → 미조정 종가로 계산 (기업행위 왜곡 주의)
    data["TDD_CLSPRC"] = pd.to_numeric(data["TDD_CLSPRC"], errors="coerce")
    close = data.pivot(index="BAS_DD", columns="stock_code", values="TDD_CLSPRC")
    return close.pct_change() * 100


# step 6: putting everything together in a function
def run_strategy(basket, sell_schedule, recovery_rate=1.0, reinvest=False, end_date=None):

    # 1. day-0 setup   ← compute n, investable, per_stock, holdings, cash
    n = len(basket.columns)  # number of stock codes listed
    investable = INITIAL_CASH / (1 + COST_RATE)

    # 종목별 매수금액
    per_stock = investable / n

    # 최초 매수거래비용
    entry_cost = investable * COST_RATE  # ~ 9,990.0

    # 현금 잔액
    cash = INITIAL_CASH - investable - entry_cost  # should land on 0

    # 보유가치
    holdings = pd.Series(data=per_stock, index=basket.columns)

    # get the days to walk
    start = "2016-01-04"
    if end_date is None:
        end_date = basket.index[-1]
    dates = basket.index[(basket.index >= start) & (basket.index <= end_date)]  # start from the first trading day of 2016

    # detecting delistings
    window_end = dates[-1]
    delist_dates = {}
    for s in basket.columns:
        last = basket[s].last_valid_index()
        if last is not None and last < window_end:
            delist_dates[s] = last

    # 2. daily loop  (dates, the loop, record W)
    records = []
    for date in dates:
        # update cash with daily interest
        cash = cash * (1 + CASH_DAILY_RATE)

        # mark-to-market the holdings (NaN return on a delisted/halted day -> 0%)
        holdings = holdings * (1 + basket.loc[date].fillna(0) / 100)

        # process sells
        to_sell = [s for s, d in sell_schedule.items() if d == date]
        for s in to_sell:
            proceeds = holdings[s]
            sell_cost = proceeds * COST_RATE
            cash += proceeds - sell_cost
            holdings[s] = 0  # gone; from now it sits in cash, not the market

        # forced delisting sells (at recovery_rate), only if still held
        for s in [s for s, d in delist_dates.items() if d == date]:
            if holdings[s] > 0:
                proceeds = holdings[s] * recovery_rate
                cash += proceeds - proceeds * COST_RATE
                holdings[s] = 0

        # record W
        W = holdings.sum() + cash
        records.append(W)
    return pd.Series(data=records, index=dates)
