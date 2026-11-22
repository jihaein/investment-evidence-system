"""Load the LLM company/macro quarterly table (정민's sheet export) into Supabase.

Usage (from the repo root):
    python backend/llm/load_company_quarters.py data/llm/company_quarters_2024Q1-2026Q3.csv --dry-run
    python backend/llm/load_company_quarters.py data/llm/company_quarters_2024Q1-2026Q3.csv

Writes only to its own table `llm_company_quarters`; the serving tables
(stocks, prices, signals, refresh_runs) are not touched. Re-running upserts.
Column names follow the FDS graph node_ids (revenue, cogs, capex, gdp, ...).
"""
import argparse
import json
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]

RENAME = {
    "종목코드": "stock_code", "기업명": "stock_name", "원명단": "in_original_list",
    "교체v2명단": "in_replacement_v2_list", "결산월": "fiscal_month", "분기": "quarter",
    "공시상태": "filing_status", "재무제표": "fs_div", "공시 접수일": "disclosed_date",
    "공시 접수번호": "rcept_no", "보고서 종류": "report_type", "기업 고유번호": "corp_code",
    "통화": "currency",
    "gdp_실질_계절조정_십억원": "gdp_real_sa_bn_krw",
    "household_consumption_실질_계절조정_십억원": "household_consumption_real_sa_bn_krw",
    "household_consumption_원계열_전년동기비_pct": "household_consumption_yoy_pct",
    "inflation_전년동기비_pct": "inflation_yoy_pct",
}
AMOUNTS = ["revenue", "operating_profit", "cogs", "operating_cash_flow", "cash", "capex", "debt",
           "shares_outstanding"]
FLOATS = ["leverage", "gdp_real_sa_bn_krw", "household_consumption_real_sa_bn_krw",
          "household_consumption_yoy_pct", "inflation_yoy_pct"]

DDL = """
CREATE TABLE IF NOT EXISTS llm_company_quarters (
    stock_code text NOT NULL,
    quarter text NOT NULL,
    stock_name text,
    in_original_list boolean,
    in_replacement_v2_list boolean,
    fiscal_month integer,
    filing_status text,
    fs_div text,
    disclosed_date date,
    rcept_no text,
    report_type text,
    corp_code text,
    currency text,
    revenue bigint, operating_profit bigint, cogs bigint, operating_cash_flow bigint,
    cash bigint, capex bigint, debt bigint, shares_outstanding bigint,
    leverage double precision,
    gdp_real_sa_bn_krw double precision,
    household_consumption_real_sa_bn_krw double precision,
    household_consumption_yoy_pct double precision,
    inflation_yoy_pct double precision,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_code, quarter)
);
ALTER TABLE llm_company_quarters ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS read_llm_company_quarters ON llm_company_quarters;
CREATE POLICY read_llm_company_quarters ON llm_company_quarters FOR SELECT USING (true);
"""


def clean(path):
    df = pd.read_csv(path, dtype=str, keep_default_na=False).rename(columns=RENAME)
    missing = {"stock_code", "quarter", *AMOUNTS, *FLOATS} - set(df.columns)
    if missing:
        raise SystemExit(f"Unexpected file layout, missing columns: {sorted(missing)}")
    # Spreadsheet export drops leading zeros and turns IDs into floats.
    df["stock_code"] = df["stock_code"].str.strip().str.zfill(6)
    df["corp_code"] = df["corp_code"].str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(8)
    df["rcept_no"] = df["rcept_no"].str.strip().str.replace(r"\.0$", "", regex=True)
    bad = df["rcept_no"].ne("") & ~df["rcept_no"].str.fullmatch(r"\d{14}")
    if bad.any():
        raise SystemExit(f"{bad.sum()} invalid receipt numbers, e.g. {df.loc[bad, 'rcept_no'].iloc[0]}")
    if df.duplicated(["stock_code", "quarter"]).any():
        raise SystemExit("Duplicate (stock_code, quarter) rows")
    for col in ("in_original_list", "in_replacement_v2_list"):
        df[col] = df[col].map({"1": True, "0": False, "": None})
    df["fiscal_month"] = pd.to_numeric(df["fiscal_month"], errors="raise").astype("Int64")
    for col in AMOUNTS:
        df[col] = pd.to_numeric(df[col].str.replace(",", ""), errors="raise").round().astype("Int64")
    for col in FLOATS:
        df[col] = pd.to_numeric(df[col], errors="raise")
    df["disclosed_date"] = pd.to_datetime(df["disclosed_date"].replace("", None)).dt.date
    keep = ["stock_code", "quarter"] + [c for c in df.columns if c not in ("stock_code", "quarter")]
    df = df[keep].astype(object).where(df.notna(), None)
    for col in ("disclosed_date",):
        df[col] = df[col].map(lambda v: v.isoformat() if v is not None and not pd.isna(v) else None)
    return df


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    df = clean(args.csv)
    print(f"{len(df):,} rows, {df['stock_code'].nunique()} stocks, quarters {df['quarter'].min()}..{df['quarter'].max()}")
    print(df.head(2).T.to_string())
    if args.dry_run:
        print("Dry run: nothing written.")
        return
    load_dotenv(ROOT / ".env")
    engine = create_engine(os.environ["SUPABASE_DB_URL"], hide_parameters=True)
    cols = [c for c in df.columns]
    rows = json.dumps(df.to_dict(orient="records"), ensure_ascii=False, default=str)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in ("stock_code", "quarter"))
    with engine.begin() as connection:
        connection.exec_driver_sql(DDL)
        connection.execute(text(f"""
            INSERT INTO llm_company_quarters ({", ".join(cols)})
            SELECT {", ".join(cols)} FROM jsonb_populate_recordset(NULL::llm_company_quarters, CAST(:rows AS jsonb))
            ON CONFLICT (stock_code, quarter) DO UPDATE SET {updates}, loaded_at = now()
        """), {"rows": rows})
        total = connection.scalar(text("SELECT count(*) FROM llm_company_quarters"))
    print(f"Loaded. llm_company_quarters now has {total:,} rows.")


if __name__ == "__main__":
    main()
