"""run_models.py 와 계약 내보내기 점검. 입력 CSV는 저장소 data/ 아래에 있어야 한다(Git에는 없음).
DuPont은 SEOSEOHI_TTM_DIR, SEOSEOHI_FINAL_DATA_DIR 환경변수가 있을 때만 점검한다."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "models" / "seoseohi"))
import run_models as rm  # noqa: E402
import export_contract as ec  # noqa: E402


ANCHOR = rm.FSCORE_CHECK[0][0]          # 점수가 나오는 쌍. 전부 유보인 요청은 아래 xfail 참고


def _check(run, case, anchor=None):
    pair, want_score, want_hold = case
    pairs = [pair] if anchor is None or pair == anchor else [anchor, pair]
    result = run(pairs)
    row = result[result["stock_code"].eq(pair[0]) & result["buy_date"].eq(pair[1]) & result["eval_date"].eq(pair[2])].iloc[0]
    got = None if row["risk_score"] is None or str(row["risk_score"]) == "<NA>" else int(row["risk_score"])
    assert got == want_score
    assert want_hold in row["hold_reason"]
    assert row["interpretable"] == (want_score is not None)


@pytest.mark.parametrize("case", rm.FSCORE_CHECK)
def test_fscore_expected_scores_and_holds(case):
    _check(rm.run_fscore, case, ANCHOR)


@pytest.mark.parametrize("case", rm.OHLSON_CHECK)
def test_ohlson_expected_scores_and_holds(case):
    _check(rm.run_ohlson, case)


@pytest.mark.skipif(not (os.getenv("SEOSEOHI_TTM_DIR") and os.getenv("SEOSEOHI_FINAL_DATA_DIR")), reason="DuPont 데이터 경로 환경변수 없음")
@pytest.mark.parametrize("case", rm.DUPONT_CHECK)
def test_dupont_expected_scores_and_holds(case):
    _check(rm.run_dupont, case)


@pytest.mark.xfail(strict=True, raises=KeyError,
                   reason="fscore.evaluate_pairs는 요청한 쌍이 전부 유보이면 fiscal_year_buy 열이 없어 KeyError. fscore.py 수정 전까지 알려진 문제")
def test_fscore_all_held_request_known_bug():
    rm.run_fscore([rm.FSCORE_CHECK[3][0]])                   # after_delisting 하나만


def test_hold_is_not_zero_in_standard_table():
    table = rm.run_fscore([ANCHOR, rm.FSCORE_CHECK[2][0]])   # same_report 유보
    table = table[table["hold_reason"].eq("same_report")].reset_index(drop=True)
    assert table.loc[0, "hold_reason"] == "same_report"
    assert str(table.loc[0, "risk_score"]) == "<NA>"


def test_contract_matches_schema_and_keeps_holds_null():
    jsonschema = pytest.importorskip("jsonschema")
    pairs = [rm.FSCORE_CHECK[0][0], rm.FSCORE_CHECK[2][0], rm.OHLSON_CHECK[4][0]]
    contract = ec.build_contract(pairs, with_dupont=False)
    schema = json.loads((ROOT / "contracts" / "risk_result.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(json.loads(json.dumps(contract, allow_nan=False)))
    by_pair = {(c["stock_code"], c["buy_date"], c["eval_date"]): c for c in contract["cases"]}
    held = by_pair[rm.FSCORE_CHECK[2][0]]["models"]["fscore"]
    assert held["risk_score"] is None and held["interpretable"] is False and held["hold_reason"] == "same_report"
    assert by_pair[rm.OHLSON_CHECK[4][0]]["models"]["ohlson"]["hold_reason"] == "basis_changed"
    for case in contract["cases"]:
        assert case["integrated"]["status"] == "hold"
        assert case["models"]["dupont"]["hold_reason"] == "data_path_not_configured"
