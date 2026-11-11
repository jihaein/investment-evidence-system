"""spike: 하루치 급락을 찾아 판정까지 돌린다 (설계서 6절 운영 흐름의 첫 연결).

백테스트 표본과 같은 단계를 같은 스크립트로 차례로 돌린다. 날짜 하나(장 마감 뒤)를 넣으면:
  1. krx_prices   KRX 전종목 시세·지수 (D-550일 ~ D, 하루치 캐시는 다시 받지 않음)
  2. events --v31 그날 급락 사건 (시장모형 z ≤ -1.96, 보통주, 관리종목 제외) → 시가총액 1,000억 이상만 남김
  3. 이름 보정   잘린 KRX 약칭 → DART 공식 이름 (sample_select --fix-names)
  4. crawl_plan + naver_crawl  사건 창·기준 기간 뉴스 (2023~2025 표본과 같은 축소 설정 + 외부 언론사 본문 --external)
  5. np_batch     2차 N·P
  6. dart         필수 공시 단위 + 실적 공시 F1
  7. ach          3차 판정 (gpt-5-mini, --market-only-holds --min-support-ratio 0.25)
  8. export       contracts/sudden_result_live_<날짜>.json
산출물은 models/sudden/cache/live/<날짜>/ (사건표) 와 cache/naver_live/ (뉴스·점수·판정). 모두 로컬 전용이다.

[시점] 판정 정보 마감은 급락일 15:30이다. KRX 일별 시세가 확정된 뒤(대개 16시 이후) 돌린다.
[한계] 네이버 검색 API 키가 없어 날짜 지정 웹 검색(naver_crawl)을 쓴다. P의 여러 종목 기준 분포는 이 캐시에 쌓인 만큼만 쓴다.

    python models/sudden/live.py --date 2026-11-09
    python models/sudden/live.py --date 2026-11-09 --from-step np     # 중간부터 다시
    python models/sudden/live.py --date 2026-11-09 --max-events 5     # 시험용: 시가총액 큰 순 5건만
    python models/sudden/live.py --date today --log --skip-done       # 매일 예약 실행 (run_live_daily.cmd)

[매일 실행] 주말·휴장일(시세에 그날이 없음)은 건너뛰고, 사건이 없으면 events 단계에서 끝낸다.
  진행 상태는 cache/live/<날짜>/run_status.json(result: running·done·no_events·market_closed·failed),
  출력은 cache/live/logs/<날짜>.log. cache/live/live.lock으로 두 실행이 겹치지 않게 한다.
  끝나면 다른 운영 날짜의 550일 시세 파일(약 150MB)을 지운다.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PY = sys.executable
NEWS_CACHE = HERE / 'cache/naver_live'
BASELINE_DAYS = 200          # 뉴스 기준 기간(N 180일 + 여유)
PRICE_DAYS = 550             # 시장모형 120거래일 + K4 260거래일
STEPS = ('prices', 'events', 'names', 'crawl', 'np', 'dart', 'ach', 'export')
CLOSE_HOUR = 16              # 오늘 날짜는 이 시각(한국) 이후에만 돌린다
STALE_HOURS = 12             # 이보다 오래된 자물쇠는 끝나지 못한 실행으로 본다


def paths(date):
    d = pd.Timestamp(date)
    start = d - pd.Timedelta(days=PRICE_DAYS)
    tag = f'{d:%Y%m%d}'
    return {'tag': tag, 'out': HERE / 'cache/live' / tag,
            'prices': HERE / f'cache/krx/prices_{start:%Y%m%d}_{tag}.csv',
            'index': HERE / f'cache/krx/index_{start:%Y%m%d}_{tag}.csv',
            'price_start': f'{start:%Y-%m-%d}', 'news_start': f'{d - pd.Timedelta(days=BASELINE_DAYS):%Y-%m-%d}',
            'ach': NEWS_CACHE / 'ach' / tag}


def env_for(p):
    env = dict(os.environ)
    env.update({'SUDDEN_OUTPUT': str(p['out'].relative_to(HERE)), 'SUDDEN_PRICES': str(p['prices']),
                'SUDDEN_INDEX': str(p['index']), 'PYTHONIOENCODING': 'utf-8'})
    return env


def keep_live_events(out, day, max_events=None):
    """events.py 결과 중 그날·시가총액 1,000억 이상·뉴스 창이 있는 사건만 남긴다. 전체는 *_all.csv로 보관."""
    ev_all, ss_all = out / 'drop_events_all.csv', out / 'drop_sessions_all.csv'
    (out / 'stock_names_krx.csv').unlink(missing_ok=True)      # events.py가 이름표를 새로 썼으니 이름 보정을 다시 한다
    (out / 'drop_events.csv').replace(ev_all)
    (out / 'drop_sessions.csv').replace(ss_all)
    events = pd.read_csv(ev_all, dtype=str, encoding='utf-8-sig')
    sessions = pd.read_csv(ss_all, dtype=str, encoding='utf-8-sig')
    ok = events[(events['D'] == day) & events['mktcap_ok'].str.lower().eq('true') & events['news_window_start'].notna()].copy()
    ok = ok.sort_values('mktcap_prev_eok', key=lambda s: pd.to_numeric(s, errors='coerce'), ascending=False)
    if max_events:
        ok = ok.head(max_events)
    ok = ok.sort_values('stock_code')
    ok.to_csv(out / 'drop_events.csv', index=False, encoding='utf-8-sig')
    sessions[sessions['drop_id'].isin(ok['drop_id'])].to_csv(out / 'drop_sessions.csv', index=False, encoding='utf-8-sig')
    return {'all_events': int((events['D'] == day).sum()), 'kept': len(ok)}


def commands(date, p):
    rel = lambda path: str(Path(path).relative_to(HERE))
    return {
        'prices': [['krx_prices.py', '--start', p['price_start'], '--end', date]],
        'events': [['events.py', '--v31', '--start', date, '--end', date]],
        'names': [['sample_select.py', '--fix-names']],
        'crawl': [['crawl_plan.py', '--max-priority', '3', '--baseline-every', '7'],
                  ['naver_crawl.py', '--stock', 'all', '--start', p['news_start'], '--end', date, '--max-priority', '3',
                   '--max-pages', '10', '--baseline-max-pages', '3', '--capped-ok', '--title-match', '--skip-automated', '--external',
                   '--out', rel(NEWS_CACHE)]],
        'np': [['np_batch.py', '--cache', rel(NEWS_CACHE)]],
        'dart': [['dart.py']],
        'ach': [['ach.py', '--cache', rel(NEWS_CACHE), '--model', 'gpt-5-mini', '--market-only-holds', '--min-support-ratio', '0.25', '--out', rel(p['ach'])]],
        'export': [['export_sudden_contract.py', '--ach', rel(p['ach']), '--events', rel(p['out']), '--name', f"live_{p['tag']}"]],
    }


def now_kst():
    return pd.Timestamp.now(tz='Asia/Seoul').tz_localize(None)


def resolve_date(text, now=None):
    """'today'면 한국 시각 오늘. 오늘 날짜는 시세가 확정되는 CLOSE_HOUR 이후에만 돌린다(장중 시세가 캐시에 남지 않게)."""
    now = now or now_kst()
    day = now.normalize() if text == 'today' else pd.Timestamp(text)
    if day > now.normalize() or (day == now.normalize() and now.hour < CLOSE_HOUR):
        raise SystemExit(f'{day:%Y-%m-%d}: 아직 시세가 확정되지 않았다({CLOSE_HOUR}시 이후에 돌린다)')
    return f'{day:%Y-%m-%d}'


def traded(p, date):
    """시세 파일에 그날 행이 있으면 거래일."""
    if not p['prices'].exists():
        return False
    days = pd.read_csv(p['prices'], usecols=['BAS_DD'], dtype=str, encoding='utf-8-sig')['BAS_DD']
    return (days == pd.Timestamp(date).strftime('%Y%m%d')).any()


def prune_price_windows(keep_tag):
    """운영 실행마다 550일 시세 파일(약 150MB)이 생긴다. 다른 운영 날짜의 파일은 지운다(하루치 캐시로 금방 다시 만든다)."""
    removed = []
    for f in (HERE / 'cache/krx').glob('*_*_*.csv'):
        tag = f.stem.rsplit('_', 1)[-1]
        if f.stem.split('_')[0] in ('prices', 'index') and tag != keep_tag and (HERE / 'cache/live' / tag).is_dir():
            f.unlink()
            removed.append(f.name)
    return removed


class Lock:
    """동시에 두 번 돌지 않게 한다. 자물쇠가 STALE_HOURS보다 오래됐으면 끝나지 못한 실행으로 보고 치운다."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and (now_kst().timestamp() - self.path.stat().st_mtime) > STALE_HOURS * 3600:
            self.path.unlink()
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise SystemExit(f'다른 실행이 진행 중이다: {self.path} ({self.path.read_text(encoding="utf-8", errors="ignore")})')
        os.write(fd, f'pid {os.getpid()} {now_kst():%Y-%m-%d %H:%M}'.encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def write_status(p, **kw):
    path = p['out'] / 'run_status.json'
    old = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    path.write_text(json.dumps({**old, **kw}, ensure_ascii=False, indent=1), encoding='utf-8')


def run(date, from_step='prices', max_events=None, runner=subprocess.run, stdout=None):
    p = paths(date)
    p['out'].mkdir(parents=True, exist_ok=True)
    env = env_for(p)
    log = []
    if pd.Timestamp(date).weekday() >= 5:
        print(f'{date}: 주말이라 건너뛴다.')
        write_status(p, date=date, result='market_closed', finished=f'{now_kst():%Y-%m-%dT%H:%M:%S}')
        return [{'step': 'calendar', 'result': 'market_closed'}]
    write_status(p, date=date, result='running', started=f'{now_kst():%Y-%m-%dT%H:%M:%S}', from_step=from_step,
                 finished=None, error=None)
    try:
        for step in STEPS[STEPS.index(from_step):]:
            for cmd in commands(date, p)[step]:
                print(f'[{step}] {" ".join(cmd)}', flush=True)
                res = runner([PY, *cmd], cwd=HERE, env=env, stdout=stdout, stderr=stdout and subprocess.STDOUT)
                if res.returncode != 0:
                    raise SystemExit(f'{step} 단계 실패 (종료 코드 {res.returncode}): {" ".join(cmd)}')
            write_status(p, last_step=step)
            if step == 'prices' and not traded(p, date):
                print(f'{date}: 시세에 그날이 없다(휴장일). 여기서 끝낸다.', flush=True)
                write_status(p, result='market_closed', finished=f'{now_kst():%Y-%m-%dT%H:%M:%S}')
                return log + [{'step': 'prices', 'result': 'market_closed'}]
            if step == 'events':
                kept = keep_live_events(p['out'], date, max_events)
                log.append({'step': 'events', **kept})
                write_status(p, events=kept)
                print(f'[events] 그날 사건 {kept["all_events"]}건 중 {kept["kept"]}건', flush=True)
                if not kept['kept']:
                    print('급락 사건이 없다. 여기서 끝낸다.')
                    write_status(p, result='no_events', finished=f'{now_kst():%Y-%m-%dT%H:%M:%S}')
                    return log
    except SystemExit as e:
        write_status(p, result='failed', error=str(e), finished=f'{now_kst():%Y-%m-%dT%H:%M:%S}')
        raise
    summary = p['ach'] / 'summary.json'
    verdicts = json.loads(summary.read_text(encoding='utf-8')).get('verdicts') if summary.exists() else None
    write_status(p, result='done', verdicts=verdicts, finished=f'{now_kst():%Y-%m-%dT%H:%M:%S}',
                 pruned=prune_price_windows(p['tag']))
    return log


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--date', required=True, help="급락일 YYYY-MM-DD 또는 today (한국 시각, 장 마감 뒤)")
    ap.add_argument('--from-step', choices=STEPS, default='prices')
    ap.add_argument('--max-events', type=int, help='시가총액 큰 순으로 이만큼만 (시험용)')
    ap.add_argument('--log', action='store_true', help='출력을 cache/live/logs/<날짜>.log에 쓴다 (예약 실행용)')
    ap.add_argument('--skip-done', action='store_true', help='그날 run_status가 done·no_events·market_closed면 다시 돌리지 않는다')
    args = ap.parse_args(argv)
    date = resolve_date(args.date)
    p = paths(date)
    status = p['out'] / 'run_status.json'
    if args.skip_done and status.exists() and \
            json.loads(status.read_text(encoding='utf-8')).get('result') in ('done', 'no_events', 'market_closed'):
        print(f'{date}: 이미 끝난 실행이다. 건너뛴다.')
        return 0
    log_file = None
    if args.log:
        (HERE / 'cache/live/logs').mkdir(parents=True, exist_ok=True)
        log_file = open(HERE / 'cache/live/logs' / f"{p['tag']}.log", 'a', encoding='utf-8')
        sys.stdout = sys.stderr = log_file
        print(f'=== {now_kst():%Y-%m-%d %H:%M:%S} live.py --date {date}', flush=True)
    try:
        with Lock(HERE / 'cache/live/live.lock'):
            print(json.dumps(run(date, args.from_step, args.max_events, stdout=log_file), ensure_ascii=False), flush=True)
    finally:
        if log_file:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
            log_file.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
