"""Compute 서서히 signals with an explicit evaluation date for live jobs."""
import argparse
import math
import os
import sys
from datetime import date
from pathlib import Path
from uuid import uuid4

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
if not __package__:
    sys.path.insert(0, str(ROOT))

from models.seoseohi.fscore import list_snapshot_dates
from models.seoseohi.ohlson.ohlson import load_mktcap
from models.seoseohi.pipeline import integrate, integrate_sum, load_data_pit, run_seoseohi_pit

RULE_VERSION = "seoseohi-sum-v1"


def json_value(value):
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def compute(base_dir, stock_codes, eval_dates=None):
    data = load_data_pit(base_dir)
    data.update(market=load_mktcap(base_dir), median_cache={})
    snapshots = [date.fromisoformat(name) for name in list_snapshot_dates(base_dir)]
    if not snapshots:
        raise ValueError("No point-in-time snapshots")
    evaluation_dates = snapshots if eval_dates is None else sorted(set(eval_dates))
    rows = []
    for evaluated in evaluation_dates:
        eligible = [snapshot for snapshot in snapshots if snapshot <= evaluated]
        for baseline in eligible:
            if eval_dates is None and baseline == evaluated:
                continue
            for stock_code in stock_codes:
                result = run_seoseohi_pit(data, {"stock_code": stock_code, "buy_date": baseline}, evaluated)
                scores = {name: json_value(value) for name, value in result["scores"].items()}
                valid = [int(value) for value in scores.values() if value is not None]
                evidence = {"financial_as_of": eligible[-1].isoformat()}
                for name in scores:
                    detail = {key: json_value(value) for key, value in result[name]["detail"].items()}
                    reason = detail.get("hold_reason") or detail.get("note") or "insufficient_data"
                    status = "held" if scores[name] is None else ("partial" if detail.get("partial") else "scored")
                    evidence[name] = {"status": status, "hold_reason": reason if status == "held" else None,
                                      "detail": detail}
                rows.append({
                    "stock_code": stock_code, "baseline_date": baseline, "eval_date": evaluated,
                    "dupont_score": scores["dupont"], "ohlson_score": scores["ohlson"],
                    "fscore": scores["fscore"], "sum_total": sum(valid) if valid else None,
                    "verdict_count": integrate(scores), "verdict_sum": integrate_sum(scores),
                    "rule_version": RULE_VERSION, "evidence": evidence,
                })
    if not rows:
        raise ValueError("No signals for the requested evaluation dates")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-date", type=date.fromisoformat, action="append")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    from data_pipeline.publication import publish

    engine = create_engine(os.environ["SUPABASE_DB_URL"])
    base = Path(os.environ["RAWDATA_DIR"])
    universe = pd.read_csv(base / "12_기업개황_KSIC.csv", skiprows=[1], dtype=str)
    rows = compute(base, universe["stock_code"].tolist(), args.eval_date)
    with engine.begin() as connection:
        run_id = publish(connection, f"signals:{uuid4()}", [], rows)
    print(f"Published {len(rows):,} signals (release {run_id})")


if __name__ == "__main__":
    main()
