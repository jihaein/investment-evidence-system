# check_q4_mismatch.py
# 4분기 TTM ≠ 연간 순이익인 6건을, 04(보완 후)와 04B(보완 전)로 분기별 비교한다

from pathlib import Path
import pandas as pd

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "ohlson"

CASES = [               # (종목코드, 연도)
    ("010170", 2016),   # 대한광통신
    ("050540", 2016),   # 엠피씨
    ("050540", 2018),
    ("050540", 2019),
    ("052270", 2015),   # 우전앤한단
    ("082800", 2019),   # 루미마이크로
]

KEEP = ["stock_code", "bsns_year", "quarter", "fs_div",
        "ifrs_ProfitLossAttributableToOwnersOfParent_quarter_amount",
        "reported_profitloss_quarter_amount", "owner_value_source", "quarter_amount_method"]


def read(name):
    df = pd.read_csv(DATA_DIR / name, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df = df[KEEP].copy()
    df["bsns_year"] = df["bsns_year"].astype(int)
    for col in ["ifrs_ProfitLossAttributableToOwnersOfParent_quarter_amount", "reported_profitloss_quarter_amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


after = read("04_재무_Ohlson용_2015-2019.csv").rename(columns={
    "ifrs_ProfitLossAttributableToOwnersOfParent_quarter_amount": "owner_ni_04",
    "owner_value_source": "source_04"})
before = read("04B_Ohlson_분기보고서_원천입력_2015-2019.csv").rename(columns={
    "ifrs_ProfitLossAttributableToOwnersOfParent_quarter_amount": "owner_ni_04B",
    "reported_profitloss_quarter_amount": "reported_04B",
    "owner_value_source": "source_04B",
    "quarter_amount_method": "method_04B",
    "fs_div": "fs_div_04B"})

both = after.merge(before, on=["stock_code", "bsns_year", "quarter"], how="left")
both["changed"] = both["owner_ni_04"] != both["owner_ni_04B"]   # 보완하면서 바뀐 분기

pd.set_option("display.max_columns", None)
pd.set_option("display.width", None)
pd.set_option("display.float_format", "{:,.0f}".format)

for code, year in CASES:
    rows = both[(both["stock_code"] == code) & (both["bsns_year"] == year)]
    print(f"\n===== {code} {year} =====")
    print(rows[["quarter", "fs_div", "owner_ni_04", "owner_ni_04B",
                "reported_profitloss_quarter_amount", "changed", "source_04", "method_04B"]].to_string(index=False))
    print("합계  04 지배주주:", f"{rows['owner_ni_04'].sum():,.0f}",
          " / 04 보고된 전체:", f"{rows['reported_profitloss_quarter_amount'].sum():,.0f}")