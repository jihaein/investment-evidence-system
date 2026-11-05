import os
from dotenv import load_dotenv
import pandas as pd

INITIAL_CASH = 10_000_000
COST_RATE = 0.001  # 0.1% transaction fee
CASH_DAILY_RATE = 0.000034

def load_daily_price() -> pd.DataFrame:
    # Load daily price & 등락률 data
    load_dotenv()

    # Get the Google Drive path
    path = os.getenv("DAILY_PRICE_DATA_DIR")
    file_path = os.path.join(path, "05_주가_일별_2015-2019.csv")

    # Read the file data
    data = pd.read_csv(file_path, header=0, skiprows=[1], dtype=str, encoding="utf-8-sig")

    data[['TDD_CLSPRC', 'CMPPREVDD_PRC', 'FLUC_RT', 'TDD_OPNPRC', 'TDD_HGPRC', 'TDD_LWPRC', 'ACC_TRDVOL', 'ACC_TRDVAL', 'MKTCAP', 'LIST_SHRS']] = data[['TDD_CLSPRC', 'CMPPREVDD_PRC', 'FLUC_RT', 'TDD_OPNPRC', 'TDD_HGPRC', 'TDD_LWPRC', 'ACC_TRDVOL', 'ACC_TRDVAL', 'MKTCAP', 'LIST_SHRS']].apply(pd.to_numeric, errors='coerce')
    data[['BAS_DD']] = data[['BAS_DD']].apply(pd.to_datetime, errors='coerce')
    data["ISU_CD"] = data["ISU_CD"].str.zfill(6)

    data = data.pivot(index='BAS_DD', columns='ISU_CD', values='FLUC_RT')

    return data

# step 6: putting everything together in a function
def run_strategy(basket, sell_schedule):    
    
    # 1. day-0 setup   ← your Step 2 code (compute n, investable, per_stock, holdings, cash)
    n = len(basket.columns) # number of stock codes listed
    investable = INITIAL_CASH / (1 + COST_RATE)

    # 종목별 매수금액 
    per_stock = investable / n

    # 최초 매수거래비용
    entry_cost = investable * COST_RATE # ~ 9,990.0

    # 현금 잔액 
    cash = INITIAL_CASH - investable - entry_cost # should land on 0

    # 보유가치
    holdings = pd.Series(data=per_stock, index=basket.columns)

    # 2. daily loop  (dates, the loop, record W)

    # get the days to walk
    dates = basket.index[basket.index >= '2016-01-04']  # start from the first trading day of 2016

    records = []
    for date in dates:
        # update cash with daily interest
        cash = cash * (1 + CASH_DAILY_RATE)

        # mark-to-market the holdings
        holdings = holdings * (1 + basket.loc[date] / 100)

        # process sells
        to_sell = [s for s, d in sell_schedule.items() if d == date]
        for s in to_sell:
            proceeds = holdings[s]
            sell_cost = proceeds * COST_RATE
            cash += proceeds - sell_cost
            holdings[s] = 0 # gone; from now it sits in cash, not the market

        # record W
        W = holdings.sum() + cash
        records.append(W)
    ser_sell_cash = pd.Series(data=records, index=dates)
    return ser_sell_cash
