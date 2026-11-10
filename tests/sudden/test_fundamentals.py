"""사후 펀더멘털 검증: 누적 → 분기값, TTM 전후, 주당 이익 변화를 외부 접속 없이 확인한다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import fundamentals as fd


def test_cumulative_reports_become_quarters():
    assert fd.quarterly({1: 10, 2: 25, 3: 30, 4: 50}) == {1: 10, 2: 15, 3: 5, 4: 20}
    assert fd.quarterly({1: 10, 2: None, 3: 30, 4: 50}) == {1: 10, 2: None, 3: None, 4: 20}


def test_cumulative_op_prefers_consolidated_and_cumulative_amount():
    data = {'list': [{'account_nm': '영업이익', 'fs_div': 'OFS', 'thstrm_amount': '5', 'thstrm_add_amount': '9'},
                     {'account_nm': '영업이익', 'fs_div': 'CFS', 'thstrm_amount': '1,000', 'thstrm_add_amount': '3,000'}]}
    assert fd.cumulative_op(data) == 3000
    assert fd.cumulative_op({'list': [{'account_nm': '영업이익', 'fs_div': 'OFS', 'thstrm_amount': '700'}]}) == 700


def test_event_metric_uses_four_quarters_around_drop_and_dilution():
    series = {(2023, q): 10.0 for q in (1, 2, 3, 4)} | {(2024, q): 5.0 for q in (1, 2, 3, 4)} | {(2025, 1): 5.0}
    px = {'price_prev': 100.0, 'shares_before': 1.0, 'shares_after': 2.0}
    m = fd.event_metric(series, px, '2024-01-15')            # 2024Q1 급락: 전 2023Q1~Q4, 후 2024Q2~2025Q1
    assert m['ttm_before'] == 40 and m['ttm_after'] == 20
    assert abs(m['dEPS_op'] - (20 / 2 - 40 / 1) / 100) < 1e-12 and m['after_end_quarter'] == '2025Q1'
    assert m['dOP_mcap'] == (20 - 40) / 100 and m['dilution'] == 1.0
    assert fd.event_metric(series, px, '2025-01-15') is None  # 뒤 4개 분기가 아직 없음
