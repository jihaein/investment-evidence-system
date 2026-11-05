"""
export_contract.py — 서서히 모델 결과를 대시보드가 읽는 계약 파일(JSON)로 내보낸다

[무엇을 하나]
run_models.py의 표준 표(F-Score, Ohlson, DuPont)를 (종목, 매수일, 평가일) 단위로 묶어
contracts/risk_result.json 으로 저장한다. 대시보드는 이 파일을 읽기만 하고 점수를 다시 계산하지 않는다.
형식은 contracts/risk_result.schema.json 을 따른다.

[원칙]
- 유보는 0점이 아니다. risk_score는 null이고 hold_reason에 사유가 남는다.
- 통합 판정(integrated)은 팀 결정 D10 전까지 항상 hold다. 모델 점수를 합치지 않는다.
- DuPont은 데이터 경로 환경변수(SEOSEOHI_TTM_DIR, SEOSEOHI_FINAL_DATA_DIR)가 없으면 계산하지 않고 사유를 남긴다.

[직접 실행]
    python export_contract.py                      # 기본 쌍으로 contracts/risk_result.json 생성
    python export_contract.py --out <경로>          # 저장 위치 지정
    python export_contract.py --buy 2016-06-30 2017-06-30 --eval 2019-12-30
"""

import argparse
import json
import math
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_models as rm

SCHEMA_VERSION = "0.1"
RULE_VERSION = "provisional-0"
# 아직 팀이 정하지 않은 항목. 결과의 의미를 제한하므로 파일에 함께 남긴다
OPEN_DECISIONS = ["D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D09", "D10"]

DEFAULT_BUY = ["2016-06-30", "2017-06-30"]
DEFAULT_EVAL = ["2019-12-30"]
DEFAULT_OUT = rm.HERE.parents[1] / "contracts" / "risk_result.json"

MODEL_RUNNERS = {"fscore": rm.run_fscore, "ohlson": rm.run_ohlson, "dupont": rm.run_dupont}
DETAIL_COLUMNS = {"fscore": rm.FSCORE_DETAIL, "ohlson": rm.OHLSON_DETAIL, "dupont": rm.DUPONT_DETAIL}


def plain(value):
    """표의 값을 JSON에 넣을 수 있는 값으로 바꾼다. 결측은 null."""
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if hasattr(value, "item"):                                  # numpy 숫자/불리언
        value = value.item()
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d")
    return value


def git_info():
    repo = rm.HERE.parents[1]

    def run(*args):
        try:
            return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            return ""
    head = run("rev-parse", "HEAD")
    dirty = bool(run("status", "--porcelain", "--", "models"))
    return head or None, dirty


def default_stock_codes():
    table = rm.build_fscore_table()
    return sorted(table["stock_code"].unique())


def dupont_configured():
    return bool(os.getenv("SEOSEOHI_TTM_DIR") and os.getenv("SEOSEOHI_FINAL_DATA_DIR"))


def model_entry(row, model):
    score = plain(row["risk_score"])
    return {
        "risk_score": score,
        "risk_max": plain(row["risk_max"]),
        "interpretable": score is not None,
        "hold_reason": plain(row["hold_reason"]) or None,
        "scale_status": plain(row["scale_status"]),
        "evidence": {col: plain(row[col]) for col in DETAIL_COLUMNS[model] if col in row.index},
    }


def build_contract(pairs, with_dupont=None):
    if with_dupont is None:
        with_dupont = dupont_configured()
    models = ["fscore", "ohlson"] + (["dupont"] if with_dupont else [])
    cases = {}
    for pair in pairs:
        cases[pair] = {"stock_code": pair[0], "buy_date": pair[1], "eval_date": pair[2], "models": {}}
    for model in models:
        table = MODEL_RUNNERS[model](pairs)
        for _, row in table.iterrows():
            key = (row["stock_code"], row["buy_date"], row["eval_date"])
            cases[key]["models"][model] = model_entry(row, model)
    if not with_dupont:
        for case in cases.values():
            case["models"]["dupont"] = {
                "risk_score": None, "risk_max": rm.DUPONT_RISK_MAX, "interpretable": False,
                "hold_reason": "data_path_not_configured", "scale_status": "provisional", "evidence": {},
            }
    for case in cases.values():
        case["integrated"] = {"status": "hold", "reason": "D10_undecided"}
    head, dirty = git_info()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_commit": head,
        "source_dirty": dirty,
        "rule_version": RULE_VERSION,
        "open_decisions": OPEN_DECISIONS,
        "models_computed": models,
        "notes": [
            "팀 결정 D01~D10이 미결정인 임시 규칙의 결과다. 확정된 위험 판정이 아니다.",
            "유보(risk_score=null)는 0점이 아니다. hold_reason을 확인한다.",
            "모델별 점수 눈금이 다르다(F-Score 0~3, Ohlson 0~2, DuPont 0~3). 합산하지 않는다.",
        ],
        "cases": list(cases.values()),
    }


def write_contract(contract, out):
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(contract, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--buy", nargs="+", default=DEFAULT_BUY)
    parser.add_argument("--eval", nargs="+", default=DEFAULT_EVAL, dest="evals")
    parser.add_argument("--stocks", nargs="+", default=None)
    args = parser.parse_args()
    stocks = args.stocks or default_stock_codes()
    pairs = [(s, b, e) for s in stocks for b in args.buy for e in args.evals if b < e]
    contract = build_contract(pairs)
    path = write_contract(contract, args.out)
    print(f"저장: {path}  사례 {len(contract['cases'])}개  모델 {contract['models_computed']}")
