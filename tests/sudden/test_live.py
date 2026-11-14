import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import pandas as pd
import pytest

import live


def _write_events(out):
    pd.DataFrame([
        {'drop_id': '000001_20261109', 'stock_code': '000001', 'D': '2026-11-09', 'mktcap_ok': 'True', 'news_window_start': 'x', 'mktcap_prev_eok': '5000'},
        {'drop_id': '000002_20261109', 'stock_code': '000002', 'D': '2026-11-09', 'mktcap_ok': 'False', 'news_window_start': 'x', 'mktcap_prev_eok': '500'},
        {'drop_id': '000003_20261109', 'stock_code': '000003', 'D': '2026-11-09', 'mktcap_ok': 'True', 'news_window_start': 'x', 'mktcap_prev_eok': '90000'},
        {'drop_id': '000004_20261106', 'stock_code': '000004', 'D': '2026-11-06', 'mktcap_ok': 'True', 'news_window_start': 'x', 'mktcap_prev_eok': '9000'},
    ]).to_csv(out / 'drop_events.csv', index=False, encoding='utf-8-sig')
    pd.DataFrame([{'drop_id': i, 'd': 0} for i in ('000001_20261109', '000002_20261109', '000003_20261109', '000004_20261106')]
                 ).to_csv(out / 'drop_sessions.csv', index=False, encoding='utf-8-sig')


def test_keep_live_events_filters_day_and_mktcap(tmp_path):
    _write_events(tmp_path)
    (tmp_path / 'stock_names_krx.csv').write_text('old', encoding='utf-8')
    assert live.keep_live_events(tmp_path, '2026-11-09') == {'all_events': 3, 'kept': 2}
    kept = pd.read_csv(tmp_path / 'drop_events.csv', dtype=str)
    assert list(kept['drop_id']) == ['000001_20261109', '000003_20261109']
    assert len(pd.read_csv(tmp_path / 'drop_sessions.csv')) == 2
    assert (tmp_path / 'drop_events_all.csv').exists() and not (tmp_path / 'stock_names_krx.csv').exists()


def test_keep_live_events_max_events_takes_largest(tmp_path):
    _write_events(tmp_path)
    live.keep_live_events(tmp_path, '2026-11-09', max_events=1)
    assert list(pd.read_csv(tmp_path / 'drop_events.csv', dtype=str)['drop_id']) == ['000003_20261109']


def test_run_order_env_and_stop_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(live, 'HERE', tmp_path)
    monkeypatch.setattr(live, 'NEWS_CACHE', tmp_path / 'cache/naver_live')
    calls = []

    def runner(cmd, cwd, env, **kw):
        calls.append((cmd[1], env['SUDDEN_OUTPUT']))
        return SimpleNamespace(returncode=1 if cmd[1] == 'dart.py' else 0)
    try:
        live.run('2026-11-09', from_step='np', runner=runner)
    except SystemExit as e:
        assert 'dart' in str(e)
    assert [c[0] for c in calls] == ['np_batch.py', 'dart.py']
    assert Path(calls[0][1]).as_posix() == 'cache/live/20261109'
    status = json.loads((tmp_path / 'cache/live/20261109/run_status.json').read_text(encoding='utf-8'))
    assert status['result'] == 'failed' and 'dart' in status['error'] and status['last_step'] == 'np'


def test_crawl_retries_then_continues(tmp_path, monkeypatch):
    calls, waits = [], []
    monkeypatch.setattr(live, 'HERE', tmp_path)
    monkeypatch.setattr(live, 'NEWS_CACHE', tmp_path / 'cache/naver_live')
    fails = {'naver_crawl.py': 2}

    def runner(cmd, cwd, env, **kw):
        calls.append(cmd[1])
        if fails.get(cmd[1], 0) > 0:
            fails[cmd[1]] -= 1
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)
    live.run('2026-11-09', from_step='crawl', runner=runner, sleep=waits.append)
    assert calls[:5] == ['crawl_plan.py', 'naver_crawl.py', 'naver_crawl.py', 'naver_crawl.py', 'np_batch.py']
    assert waits == [live.CRAWL_WAIT, live.CRAWL_WAIT]


def test_crawl_gives_up_after_tries(tmp_path, monkeypatch):
    monkeypatch.setattr(live, 'HERE', tmp_path)
    monkeypatch.setattr(live, 'NEWS_CACHE', tmp_path / 'cache/naver_live')
    calls = []

    def runner(cmd, cwd, env, **kw):
        calls.append(cmd[1])
        return SimpleNamespace(returncode=1 if cmd[1] == 'naver_crawl.py' else 0)
    with pytest.raises(SystemExit, match='crawl'):
        live.run('2026-11-09', from_step='crawl', runner=runner, sleep=lambda s: None)
    assert calls.count('naver_crawl.py') == live.CRAWL_TRIES and 'np_batch.py' not in calls
    status = json.loads((tmp_path / 'cache/live/20261109/run_status.json').read_text(encoding='utf-8'))
    assert status['result'] == 'failed' and status['retries'] == {'crawl': live.CRAWL_TRIES - 1}


def test_paths_price_window():
    p = live.paths('2026-11-09')
    assert p['tag'] == '20261109' and p['price_start'] == '2025-05-08' and p['news_start'] == '2026-04-23'


def test_resolve_date_today_only_after_close():
    after = pd.Timestamp('2026-11-11 18:30')
    assert live.resolve_date('today', after) == '2026-11-11'
    assert live.resolve_date('2026-11-09', after) == '2026-11-09'
    with pytest.raises(SystemExit):
        live.resolve_date('today', pd.Timestamp('2026-11-11 14:00'))        # 장중 시세가 캐시에 남지 않게
    with pytest.raises(SystemExit):
        live.resolve_date('2026-11-12', after)


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(live, 'HERE', tmp_path)
    monkeypatch.setattr(live, 'NEWS_CACHE', tmp_path / 'cache/naver_live')
    calls = []

    def runner(cmd, cwd, env, **kw):
        calls.append(cmd[1])
        return SimpleNamespace(returncode=0)
    return calls, runner


def test_weekend_and_holiday_stop_early(tmp_path, monkeypatch):
    calls, runner = _isolate(tmp_path, monkeypatch)
    assert live.run('2026-11-08', runner=runner)[0]['result'] == 'market_closed' and calls == []     # 일요일
    p = live.paths('2026-11-10')
    p['prices'].parent.mkdir(parents=True)
    pd.DataFrame({'BAS_DD': ['20261109']}).to_csv(p['prices'], index=False, encoding='utf-8-sig')     # 그날 행이 없음
    assert live.run('2026-11-10', runner=runner)[-1]['result'] == 'market_closed' and calls == ['krx_prices.py']
    status = json.loads((p['out'] / 'run_status.json').read_text(encoding='utf-8'))
    assert status['result'] == 'market_closed'


def test_lock_blocks_second_run_and_clears_stale(tmp_path, monkeypatch):
    path = tmp_path / 'live.lock'
    with live.Lock(path):
        with pytest.raises(SystemExit):
            with live.Lock(path):
                pass
    assert not path.exists()
    path.write_text('pid 1', encoding='utf-8')
    import os
    old = path.stat().st_mtime - (live.STALE_HOURS + 1) * 3600
    os.utime(path, (old, old))
    with live.Lock(path):
        pass


def test_prune_keeps_current_and_sample_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(live, 'HERE', tmp_path)
    krx = tmp_path / 'cache/krx'
    krx.mkdir(parents=True)
    for tag in ('20261109', '20261110'):
        (tmp_path / 'cache/live' / tag).mkdir(parents=True)
    names = ['prices_20250508_20261109.csv', 'index_20250508_20261109.csv', 'prices_20250509_20261110.csv',
             'prices_20220601_20251231.csv']
    for n in names:
        (krx / n).write_text('x', encoding='utf-8')
    assert sorted(live.prune_price_windows('20261110')) == ['index_20250508_20261109.csv', 'prices_20250508_20261109.csv']
    assert sorted(f.name for f in krx.iterdir()) == ['prices_20220601_20251231.csv', 'prices_20250509_20261110.csv']


def test_krx_does_not_cache_recent_empty_day(tmp_path, monkeypatch):
    import krx_prices
    monkeypatch.setattr(krx_prices, 'OUT', tmp_path)
    today = pd.Timestamp.now(tz='Asia/Seoul').tz_localize(None).normalize()
    empty = lambda day, mkt: None
    krx_prices.fetch_day(today, empty)
    assert not (tmp_path / 'daily' / f'{today:%Y%m%d}.csv').exists()          # 아직 확정 전일 수 있음
    old = today - pd.Timedelta(days=30)
    krx_prices.fetch_day(old, empty)
    assert (tmp_path / 'daily' / f'{old:%Y%m%d}.csv').exists()                # 지난 휴장일은 캐시
