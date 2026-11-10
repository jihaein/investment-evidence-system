"""2023~2025 대형주 표본: 기준 기간 날짜 표본 추출과 페이지 상한(capped) 처리."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import crawl_plan as cp
import naver_crawl as crawl


def inputs():
    events = pd.DataFrame([{'drop_id': 'A_1', 'stock_code': '000010', 'D': '2024-06-20',
                            'news_window_start': '2024-06-17T15:30:00+09:00', 'p_base_start': '2024-03-19T15:30:00+09:00',
                            'n_base_start': '2023-12-20T15:30:00+09:00'}])
    names = pd.DataFrame([{'stock_code': '000010', 'company_name': 'A사', 'from': pd.Timestamp('2020-01-01'),
                           'to': pd.Timestamp('2025-12-31')}])
    return events, names


def test_baseline_every_keeps_all_window_days_and_every_seventh_baseline_day():
    events, names = inputs()
    full = cp.build_plan(events, names, 3)
    sampled = cp.build_plan(events, names, 3, baseline_every=7)
    window = full[full['priority'] == 1]
    assert set(window['date']) <= set(sampled['date'])
    base = sampled[sampled['priority'] > 1]
    assert all(pd.Timestamp(d).toordinal() % 7 == 0 for d in base['date'])
    ratio = len(base) / len(full[full['priority'] > 1])
    assert 0.12 < ratio < 0.16


class FakeFetch:
    def __init__(self):
        self.n = 0

    def get(self, url):
        self.n += 1
        items = ''.join(f'<li class="bx"><a class="news_tit" href="https://example.com/{self.n}-{i}">t</a></li>' for i in range(3))
        return f'<ul class="list_news">{items}</ul><a class="btn_next" href="https://search.naver.com/p{self.n}">다음</a>', 'raw'


def test_capped_status_only_when_cap_is_intended(monkeypatch):
    monkeypatch.setattr(crawl, 'parse_search', lambda html: ([{'original_url': html[60:90], 'naver_url': None, 'title': 't'}],
                                                             'https://search.naver.com/next' + str(len(html)), False))
    status, records, _, reason = crawl.search(FakeFetch(), '"A사"', '2024-06-20', 2, cap_ok=True)
    assert status == 'capped' and reason == '페이지 상한 도달'
    status, _, _, _ = crawl.search(FakeFetch(), '"A사"', '2024-06-20', 2)
    assert status == 'incomplete'


def test_title_names_use_name_table_and_query(tmp_path, monkeypatch):
    path = tmp_path / 'stock_names.csv'
    path.write_text('stock_code,company_name,from,to\n017800,현대엘리베이,2023-01-01,2023-06-30\n'
                    '017800,현대엘리베이터,2023-07-01,2025-12-31\n', encoding='utf-8-sig')
    monkeypatch.setattr(crawl, '_NAMES', None)
    names = crawl.title_names('017800', '"현대엘리베이터"', path)
    assert names == {'현대엘리베이', '현대엘리베이터'}
    assert any(n in '현대엘리베이터, 2분기 수주 증가' for n in names)
    assert not any(n in '엘리베이터 업계 전망' for n in names)


def test_fix_names_only_expands_truncated_abbreviations():
    import sample_select as ss
    names = pd.DataFrame([{'stock_code': '017800', 'company_name': '현대엘리베이', 'from': '2023-01-01', 'to': '2025-12-31'},
                          {'stock_code': '050540', 'company_name': '엠피씨', 'from': '2016-01-01', 'to': '2017-12-31'},
                          {'stock_code': '050540', 'company_name': '한국코퍼레이션', 'from': '2018-01-01', 'to': '2019-12-31'}])
    out = ss.fix_names(names, {'017800': '현대엘리베이터', '050540': '한국코퍼레이션'})
    assert list(out['company_name']) == ['현대엘리베이터', '엠피씨', '한국코퍼레이션']
