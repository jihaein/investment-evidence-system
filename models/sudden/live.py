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


def run(date, from_step='prices', max_events=None, runner=subprocess.run):
    p = paths(date)
    p['out'].mkdir(parents=True, exist_ok=True)
    env = env_for(p)
    log = []
    for step in STEPS[STEPS.index(from_step):]:
        for cmd in commands(date, p)[step]:
            print(f'[{step}] {" ".join(cmd)}', flush=True)
            res = runner([PY, *cmd], cwd=HERE, env=env)
            if res.returncode != 0:
                raise SystemExit(f'{step} 단계 실패 (종료 코드 {res.returncode}): {" ".join(cmd)}')
        if step == 'events':
            kept = keep_live_events(p['out'], date, max_events)
            log.append({'step': 'events', **kept})
            print(f'[events] 그날 사건 {kept["all_events"]}건 중 {kept["kept"]}건', flush=True)
            if not kept['kept']:
                print('급락 사건이 없다. 여기서 끝낸다.')
                return log
    return log


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--date', required=True, help='급락일 YYYY-MM-DD (장 마감 뒤)')
    ap.add_argument('--from-step', choices=STEPS, default='prices')
    ap.add_argument('--max-events', type=int, help='시가총액 큰 순으로 이만큼만 (시험용)')
    args = ap.parse_args(argv)
    print(json.dumps(run(args.date, args.from_step, args.max_events), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
