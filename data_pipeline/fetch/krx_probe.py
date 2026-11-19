from dotenv import load_dotenv
load_dotenv("/Users/jihaein/Developer/investment-evidence-system/.env")
from pykrx import stock

df = stock.get_market_cap_by_ticker("20261117", market="KOSDAQ")   # 하루치, 전 코스닥
print("받은 종목 수:", len(df))
print("컬럼:", list(df.columns))
print(df.head())