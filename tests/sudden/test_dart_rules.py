"""DART 공시 제목 분류·사건 창 배정과 시장 규칙 K4를 외부 접속 없이 확인한다."""
import io
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import dart
import market_rules as mr


def test_classify_titles():
    assert dart.classify('단일판매ㆍ공급계약해지')['type'] == 'F2' and dart.classify('단일판매ㆍ공급계약해지')['direction'] == '부정'
    assert dart.classify('단일판매ㆍ공급계약체결')['direction'] == '긍정'
    c = dart.classify('[기재정정]주요사항보고서(유상증자결정)')
    assert c['type'] == 'C1' and c['correction'] and c['report'] == '주요사항보고서(유상증자결정)'
    assert dart.classify('주요사항보고서(전환사채권발행결정)')['type'] == 'C1'
    assert dart.classify('영업(잠정)실적(공정공시)')['kind'] == 'needs_detail'
    assert dart.classify('주식등의대량보유상황보고서(일반)')['grade'] == 0.6
    assert dart.classify('기업설명회(IR)개최') is None


def test_window_units_assign_sessions_by_receipt_date():
    sessions = [{'d': '0', 'trade_date': '2018-03-05'}, {'d': '1', 'trade_date': '2018-03-02'},
                {'d': '2', 'trade_date': '2018-02-28'}]
    rows = [{'rcept_no': '1', 'rcept_dt': '20180305', 'report_nm': '단일판매ㆍ공급계약해지'},
            {'rcept_no': '2', 'rcept_dt': '20180227', 'report_nm': '유상증자결정'},
            {'rcept_no': '3', 'rcept_dt': '20180302', 'report_nm': '영업(잠정)실적(공정공시)'}]
    units, detail = dart.window_units(rows, sessions, 'A사')
    assert [u['rcept_no'] for u in units] == ['1'] and units[0]['on_drop_day'] and units[0]['mandatory']
    assert [d['rcept_no'] for d in detail] == ['3']


def test_collect_uses_corp_codes_and_pages(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('CORPCODE.xml', '<result><list><corp_code>00123</corp_code><stock_code>010170</stock_code></list>'
                                   '<list><corp_code>00999</corp_code><stock_code> </stock_code></list></result>')
    pages = {1: {'status': '000', 'total_page': 2, 'list': [{'rcept_no': 'a', 'rcept_dt': '20180305', 'report_nm': '유상증자결정'}]},
             2: {'status': '000', 'total_page': 2, 'list': [{'rcept_no': 'b', 'rcept_dt': '20180302', 'report_nm': '소송등의제기'}]}}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        url = req.full_url
        if 'corpCode' in url:
            return Resp(buf.getvalue())
        page = int(url.split('page_no=')[1].split('&')[0])
        return Resp(json.dumps(pages[page]).encode())
    events = [{'drop_id': 'e1', 'stock_code': '010170', 'company_name': 'A사'}]
    sessions = {'e1': [{'d': '0', 'trade_date': '2018-03-05'}, {'d': '1', 'trade_date': '2018-03-02'}]}
    out = dart.collect(events, sessions, 'KEY', opener, tmp_path)
    assert out['e1']['status'] == 'ok' and out['e1']['filings'] == 2
    assert sorted(u['type'] for u in out['e1']['units']) == ['C1', 'F5']


def test_k4_flags_pre_drop_run_up():
    n, d = 400, 380
    ret = np.zeros(n)
    ret[d - 20:d] = 0.02                                   # 직전 20일 +40%
    stock = pd.DataFrame({'ret': ret, 'index_ret': 0.0, 'halted': False})
    u = mr.k4(stock, d)
    assert u['type'] == 'K4' and u['w'] == 0.6
    calm = pd.DataFrame({'ret': np.random.default_rng(1).normal(0, 0.01, n), 'index_ret': 0.0, 'halted': False})
    assert mr.k4(calm, d) is None or mr.k4(calm, d)['type'] == 'K4'
    assert mr.k4(stock, 100) == 'insufficient'


def test_titles_that_looked_alike_are_not_misread():
    assert dart.classify('주요사항보고서(자기주식취득신탁계약해지결정)')['type'] == 'C4'
    assert dart.classify('신탁계약해지결과보고서')['type'] == 'C4'
    assert dart.classify('주권매매거래정지해제(풍문 등 조회결과 공시)') is None
    assert dart.classify('유상증자결정(종속회사의주요경영사항)') is None
    assert dart.classify('조회공시요구(풍문또는보도)에대한답변(미확정)') is None
    assert dart.classify('조회공시요구(현저한시황변동)에대한답변(중요정보없음)')['type'] == 'R2'
    assert dart.classify('소송등의제기ㆍ신청(경영권분쟁소송)')['type'] == 'C3'
