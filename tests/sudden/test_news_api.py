"""news_api: 외부 접속 없이 멈춤 규칙, 덮은 구간, 날짜 나누기, 한도 초과, 수집 DB 형식을 확인한다."""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import pytest

import naver_crawl as crawl
import news_api as na

KST = na.KST


def item(t, n, naver=True):
    link = f'https://n.news.naver.com/mnews/article/001/{n:010d}' if naver else f'https://press.example/{n}'
    return {'title': f'<b>가나</b> 소식 {n}', 'originallink': f'https://press.example/{n}', 'link': link,
            'description': '요약', 'pubDate': t.strftime('%a, %d %b %Y %H:%M:%S +0900')}


class FakeSession:
    """시각 내림차순 기사 목록을 start·display로 잘라 준다."""

    def __init__(self, items, status=200):
        self.items, self.status, self.starts = items, status, []

    def get(self, url, params, headers, timeout):
        self.starts.append(params['start'])
        s = params['start'] - 1
        body = {'items': self.items[s:s + params['display']]}
        return SimpleNamespace(status_code=self.status, ok=self.status == 200, json=lambda: body, text='')


def client(items, status=200):
    return na.Client('id', 'secret', session=FakeSession(items, status), sleep=lambda s: None)


def test_first_run_reads_until_exhausted_and_covers_everything(tmp_path):
    now = datetime(2026, 11, 18, 18, 30, tzinfo=KST)
    items = [item(now - timedelta(hours=5 * i), i) for i in range(150)]
    db = na.connect(tmp_path / 'api.sqlite3')
    r = na.accumulate(client(items), db, '000001', '가나', now)
    assert r['items'] == 150 and r['exhausted']
    old, new = db.execute('SELECT oldest_at, newest_at FROM coverage').fetchone()
    assert old.startswith('2000-01-01') and new == now.isoformat()


def test_incremental_run_stops_at_previous_newest(tmp_path):
    t0 = datetime(2026, 11, 17, 18, 30, tzinfo=KST)
    db = na.connect(tmp_path / 'api.sqlite3')
    many = [item(t0 - timedelta(hours=i), i) for i in range(1200)]          # 1,000건 한도에서 끊긴다
    r1 = na.accumulate(client(many), db, '000001', '가나', t0)
    assert not r1['exhausted'] and r1['items'] == 1000
    first_old = db.execute('SELECT oldest_at FROM coverage').fetchone()[0]
    t1 = t0 + timedelta(days=1)
    new = [item(t1 - timedelta(hours=i), 5000 + i) for i in range(20)] + many  # 하루 사이 20건 추가
    c = client(new)
    r2 = na.accumulate(c, db, '000001', '가나', t1)
    assert r2['reached_prev'] and c.session.starts == [1]                    # 한 번 요청으로 끝
    assert db.execute('SELECT oldest_at FROM coverage').fetchone()[0] == first_old   # 이어진 구간이라 넓어짐


def test_gap_resets_coverage_start(tmp_path):
    t0 = datetime(2026, 11, 1, tzinfo=KST)
    db = na.connect(tmp_path / 'api.sqlite3')
    na.accumulate(client([item(t0 - timedelta(hours=i), i) for i in range(50)]), db, '000001', '가나', t0)
    t1 = t0 + timedelta(days=30)
    flood = [item(t1 - timedelta(minutes=i), 9000 + i) for i in range(1100)]   # 30일 사이 1,100건: 지난 구간에 닿지 못함
    r = na.accumulate(client(flood), db, '000001', '가나', t1)
    assert not r['reached_prev']
    old = datetime.fromisoformat(db.execute('SELECT oldest_at FROM coverage').fetchone()[0])
    assert old > t0                                                         # 빈틈 뒤만 덮은 것으로 줄어든다


def test_quota_and_auth_errors():
    with pytest.raises(na.QuotaExceeded):
        client([], status=429).page('"가나"', 1)
    with pytest.raises(SystemExit):
        client([], status=401).page('"가나"', 1)


def test_materialize_writes_crawl_format_only_for_covered_days(tmp_path):
    now = datetime(2026, 11, 18, 18, 30, tzinfo=KST)
    items = [item(datetime(2026, 11, 18, 10, 0, tzinfo=KST), 1), item(datetime(2026, 11, 18, 16, 0, tzinfo=KST), 2),
             item(datetime(2026, 11, 16, 9, 0, tzinfo=KST), 3, naver=False)]
    api = na.connect(tmp_path / 'api.sqlite3')
    na.accumulate(client(items), api, '000001', '가나', now)
    api.execute("UPDATE coverage SET oldest_at=?", (datetime(2026, 11, 15, tzinfo=KST).isoformat(),))
    cdb = crawl.connect(tmp_path / 'crawl.sqlite3')
    rows = [({'stock_code': '000001', 'date': d, 'priority': p, 'drop_ids': 'x'}, '가나')
            for d, p in (('2026-11-18', '1'), ('2026-11-16', '2'), ('2026-11-14', '3'))]
    out = na.materialize(api, cdb, rows)
    assert out == {'written': 2, 'not_covered': 1, 'kept_existing': 0}       # 11-14는 덮은 구간 밖
    rec = {json.loads(k)[1]: json.loads(p) for k, p in cdb.execute('SELECT key, payload FROM searches')}
    assert rec['2026-11-18']['status'] == 'complete' and len(rec['2026-11-18']['results']) == 2
    assert rec['2026-11-18']['results'][0]['title'] == '가나 소식 2' and rec['2026-11-18']['source'] == 'naver_api'
    ext = rec['2026-11-16']['results'][0]
    assert ext['naver_url'] is None and ext['original_url'] == 'https://press.example/3'   # 외부 기사는 원문 주소로


def test_shadow_compare_recall_and_verdicts(tmp_path):
    import live_api_shadow as sh
    ldb, sdb = crawl.connect(tmp_path / 'live.sqlite3'), crawl.connect(tmp_path / 'shadow.sqlite3')
    key = json.dumps(['000001', '2026-11-18', '가나'], ensure_ascii=False)
    res = lambda urls: [{'title': f'가나, 소식 {u}', 'naver_url': None, 'original_url': u} for u in urls]
    base = {'stock_code': '000001', 'date': '2026-11-18', 'query': '가나', 'priority': 1, 'status': 'complete'}
    crawl.save(ldb, 'searches', key, 'complete', {**base, 'results': res(['a', 'b'])})
    crawl.save(sdb, 'searches', key, 'complete', {**base, 'results': res(['a', 'c']), 'source': 'naver_api'})
    (tmp_path / 'la').mkdir(); (tmp_path / 'sa').mkdir()
    for d, v in (('la', '비가치'), ('sa', '판단보류')):
        (tmp_path / d / '000001_20261118.json').write_text(json.dumps({'verdict': v}), encoding='utf-8')
    out = sh.compare(tmp_path / 'live.sqlite3', tmp_path / 'shadow.sqlite3', '20261118', tmp_path / 'la', tmp_path / 'sa')
    assert out['recall']['event'] == {'crawl_articles': 2, 'also_in_api': 1, 'api_only': 1, 'recall': 0.5}
    assert out['verdict_agree'] == 0.0 and out['verdict_diffs'] == {'000001_20261118': {'crawl': '비가치', 'api': '판단보류'}}
