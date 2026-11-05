"""run_gate(임계 모델 경보) 점검. 입력 주가 CSV는 저장소 data/ 아래에 있어야 한다.
점수가 아니라 켜짐/꺼짐이므로 run_all에 섞이지 않는지, 유보가 False로 보이지 않는지도 함께 본다."""

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "models" / "seoseohi"))
import run_models as rm  # noqa: E402

HYUNJIN = ("053660", "2016-06-30", "2019-06-28")      # 하루 -30% 급락이 만든 경보 (2018-12-19)
DAEHAN = ("010170", "2016-06-30", "2019-06-28")


@pytest.mark.parametrize("case", rm.GATE_CHECK)
def test_gate_expected_alarms_and_holds(case):
    pair, want_first, want_days, want_hold = case
    row = rm.run_gate([pair]).iloc[0]
    first = None if pd.isna(row["first_alarm_date"]) else row["first_alarm_date"].strftime("%Y-%m-%d")
    days = None if pd.isna(row["alarm_days"]) else int(row["alarm_days"])
    assert first == want_first
    assert days == want_days
    assert want_hold in row["hold_reason"]
    assert bool(row["interpretable"]) == (want_days is not None)


def test_gate_has_no_score_columns():
    table = rm.run_gate([HYUNJIN])
    assert list(table.columns) == rm.GATE_COLUMNS
    assert "risk_score" not in table.columns and "risk_max" not in table.columns
    assert table.loc[0, "scale_status"] == "gate"


def test_run_all_does_not_include_gate():
    assert set(rm.run_all([rm.FSCORE_CHECK[0][0]])["model"]) == {"fscore", "ohlson"}


def test_gate_hold_is_blank_not_false():
    held = rm.run_gate([("052270", "2016-06-30", "2019-06-28")]).iloc[0]       # 상장폐지 이후
    assert pd.isna(held["alarm_on"]) and pd.isna(held["alarm_in_hold"])
    assert held["interpretable"] is False or bool(held["interpretable"]) is False


def test_gate_all_held_request_keeps_columns():
    table = rm.run_gate([("052270", "2016-06-30", "2019-06-28")])
    assert list(table.columns) == rm.GATE_COLUMNS


def test_gate_empty_request_returns_empty_table_with_columns():
    table = rm.run_gate([])
    assert len(table) == 0 and list(table.columns) == rm.GATE_COLUMNS


def test_gate_last_alarm_is_not_before_first_and_matches_hold_flag():
    table = rm.run_gate([HYUNJIN, DAEHAN, ("036890", "2016-06-30", "2019-06-28")])
    for _, row in table.iterrows():
        if row["alarm_in_hold"]:
            assert row["last_alarm_date"] >= row["first_alarm_date"]
            assert row["days_since_last_alarm"] >= 0
        else:
            assert pd.isna(row["last_alarm_date"])
    assert bool(table.loc[table["stock_code"] == "036890", "alarm_in_hold"].iloc[0]) is False


def test_gate_higher_h_removes_the_alarm():
    row = rm.run_gate([HYUNJIN], h=10).iloc[0]                                  # 이동 방식, k 0.5, h 10은 경보가 없다
    assert row["alarm_days"] == 0 and row["h"] == 10


def test_gate_cond3_share_removes_the_crash_driven_alarm():
    off = rm.run_gate([HYUNJIN]).iloc[0]
    share = rm.run_gate([HYUNJIN], cond3="share").iloc[0]
    assert off["alarm_days"] == 4 and share["alarm_days"] == 0
    assert share["cond3_mode"] == "share"
