import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import pandas as pd

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

    def runner(cmd, cwd, env):
        calls.append((cmd[1], env['SUDDEN_OUTPUT']))
        return SimpleNamespace(returncode=1 if cmd[1] == 'dart.py' else 0)
    try:
        live.run('2026-11-09', from_step='np', runner=runner)
    except SystemExit as e:
        assert 'dart' in str(e)
    assert [c[0] for c in calls] == ['np_batch.py', 'dart.py']
    assert Path(calls[0][1]).as_posix() == 'cache/live/20261109'


def test_paths_price_window():
    p = live.paths('2026-11-09')
    assert p['tag'] == '20261109' and p['price_start'] == '2025-05-08' and p['news_start'] == '2026-04-23'
