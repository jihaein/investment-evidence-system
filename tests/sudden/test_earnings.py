import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import earnings as en


def test_reported_period():
    assert en.reported_period('20190130', '연결재무제표기준영업(잠정)실적(공정공시)') == ('Q', 2018, 4)
    assert en.reported_period('20190425', '영업(잠정)실적(공정공시)') == ('Q', 2019, 1)
    assert en.reported_period('20191030', '영업(잠정)실적(공정공시)') == ('Q', 2019, 3)
    assert en.reported_period('20190220', '매출액또는손익구조30%(대규모법인은15%)이상변동') == ('Y', 2018, None)
    assert en.reported_period('20190814', '매출액또는손익구조30%(대규모법인은15%)이상변동') is None
    assert en.reported_period('20190220', '주식등의대량보유상황보고서') is None


def test_direction():
    assert en.direction(-5, 10) == ('부정', None)        # 적자 전환
    assert en.direction(5, -10) == ('긍정', None)        # 흑자 전환
    assert en.direction(38, 100) == ('부정', -0.62)
    assert en.direction(120, 100)[0] == '긍정'
    assert en.direction(95, 100)[0] is None             # ±10% 안
    assert en.direction(None, 100) == (None, None)


def _detail(rcept_dt, report, no='1'):
    return {'rcept_no': no, 'rcept_dt': rcept_dt, 'session_d': 0, 'time_unknown': True, 'on_drop_day': True,
            'report': report, 'correction': False, 'grade': 1.0}


def test_event_units_quarter_and_dedupe():
    cum = {2019: {1: 10e8, 2: 30e8, 3: 45e8, 4: 60e8}, 2018: {1: 20e8, 2: 50e8, 3: 80e8, 4: 100e8}}
    detail = [_detail('20191030', '영업(잠정)실적(공정공시)', '1'),
              _detail('20191030', '연결재무제표기준영업(잠정)실적(공정공시)', '2'),
              _detail('20191030', '주식등의대량보유상황보고서', '3')]
    units, skipped = en.event_units(detail, lambda y: cum[y], 'A사')
    assert len(units) == 1 and not skipped                # 같은 보고 기간은 하나로
    u = units[0]
    assert (u['type'], u['direction'], u['mandatory'], u['rcept_no']) == ('F1', '부정', True, '1')
    assert u['op_cur'] == 15e8 and u['op_prev'] == 30e8   # 3분기 = 3분기 누적 - 반기
    assert '-50%' in u['quote']


def test_event_units_small_change_and_missing_skipped():
    cum = {2019: {1: 19.5e8, 2: None, 3: None, 4: 105e8}, 2018: {1: 20e8, 2: None, 3: None, 4: 100e8}}
    detail = [_detail('20190220', '매출액또는손익구조30%(대규모법인은15%)이상변동', '1'),
              _detail('20190430', '영업(잠정)실적(공정공시)', '2')]
    units, skipped = en.event_units(detail, lambda y: cum.get(y, {}), 'A사')
    assert units == []
    assert [s['why'] for s in skipped] == ['no_numbers', 'small_change']


def test_enrich_is_idempotent():
    data = {'list': [{'account_nm': '영업이익', 'fs_div': 'CFS', 'thstrm_amount': '100'}]}
    rec = {'corp_code': 'c', 'units': [{'type': 'C1', 'source': None}],
           'needs_detail': [_detail('20190220', '매출액또는손익구조30%(대규모법인은15%)이상변동')]}
    calls = {'n': 0}

    def fetcher(key, corp, year, rep):
        calls['n'] += 1
        amt = '-50' if year == 2018 else '100'
        return {'list': [{'account_nm': '영업이익', 'fs_div': 'CFS', 'thstrm_amount': amt}]} if rep == '11011' else data
    by_event = {'e1': rec}
    s1 = en.enrich(by_event, {'e1': 'A사'}, 'k', fetcher)
    s2 = en.enrich(by_event, {'e1': 'A사'}, 'k', fetcher)
    assert s1 == s2 and s1['units'] == 1 and s1['부정'] == 1
    assert [u['type'] for u in by_event['e1']['units']] == ['C1', 'F1']
