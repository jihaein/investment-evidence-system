"""forward_ledger.py — 실시간 판정의 사후 성적표 (2026-11-20 설정 고정 뒤의 표본 외 검증).

매일 live.py가 남긴 contracts/sudden_result_live_<날짜>.json을 모아, 사건마다 매도일(급락일 다음 거래일) 종가 뒤
H 거래일 수익률과 같은 시장 지수 대비 초과수익을 붙인다. 아직 H일이 지나지 않은 칸은 비워 둔다(만기 전).
live.py·예약 실행은 건드리지 않고 그 결과 파일만 읽는다. 다시 돌리면 처음부터 다시 계산한다(멱등).

읽는 것
  contracts/sudden_result_live_*.json            판정
  cache/live/<날짜>/run_status.json               실패·휴장 날짜(빈 날을 숨기지 않게)
  cache/krx/prices_*_<가장 최근>.csv, index_*.csv  live.py가 받아 둔 최신 시세(FLUC_RT)와 KOSPI·KOSDAQ 지수
쓰는 것
  cache/live/forward_ledger.csv                  사건별 한 줄
  cache/live/forward_summary.json                판정 × 기간별 만기 사건 수·평균 초과수익·음수 비율

    .venv\\Scripts\\python.exe models\\sudden\\forward_ledger.py
"""
import argparse
import json
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CONTRACTS = ROOT / 'contracts'
LIVE = HERE / 'cache/live'
KRX = HERE / 'cache/krx'
HORIZONS = (5, 20, 60, 120, 250)
FROZEN_ON = '2026-11-20'


def latest_prices(krx=KRX):
    """가장 늦게 끝나는 prices_<시작>_<끝>.csv와 같은 이름의 index 파일."""
    files = sorted(krx.glob('prices_*_*.csv'), key=lambda p: p.stem.split('_')[-1])
    if not files:
        raise FileNotFoundError(f'시세 파일 없음: {krx}')
    prices = files[-1]
    return prices, prices.with_name(prices.name.replace('prices_', 'index_', 1))


def load_returns(prices_path):
    """날짜 × 종목코드 일간 수익률(소수). FLUC_RT는 기업행위를 반영한 거래소 공식 등락률이다."""
    d = pd.read_csv(prices_path, dtype={'BAS_DD': str, 'ISU_CD': str}, usecols=['BAS_DD', 'ISU_CD', 'FLUC_RT'],
                    encoding='utf-8-sig', low_memory=False)
    d['FLUC_RT'] = pd.to_numeric(d['FLUC_RT'].astype(str).str.replace(',', ''), errors='coerce') / 100
    d['BAS_DD'] = pd.to_datetime(d['BAS_DD'], format='%Y%m%d')
    return d.pivot_table(index='BAS_DD', columns='ISU_CD', values='FLUC_RT').sort_index()


def load_index(index_path):
    d = pd.read_csv(index_path, encoding='utf-8-sig', parse_dates=['trade_date'])
    return d.pivot_table(index='trade_date', columns='market', values='close').sort_index().pct_change()


def load_cases(contracts=CONTRACTS):
    rows = []
    for f in sorted(contracts.glob('sudden_result_live_*.json')):
        doc = json.loads(f.read_text(encoding='utf-8'))
        for c in doc['cases']:
            rows.append({'drop_id': c['drop_id'], 'stock_code': c['stock_code'], 'company_name': c.get('company_name'),
                         'market': c.get('market'), 'drop_date': c['drop_date'], 'z': c.get('z'), 'verdict': c['verdict'],
                         'hypothesis': c.get('hypothesis'), 'hold_reason': c.get('hold_reason'),
                         'rule_version': doc.get('rule_version'), 'source_file': f.name})
    return pd.DataFrame(rows)


def ledger(cases, rets, index, horizons=HORIZONS):
    days = rets.index
    out = []
    for c in cases.to_dict(orient='records'):
        later = days[days > pd.Timestamp(c['drop_date'])]
        row = {**c, 'sell_date': later[0].strftime('%Y-%m-%d') if len(later) else None}
        if len(later) and c['stock_code'] in rets.columns:
            i = days.get_loc(later[0])
            for h in horizons:
                win = days[i + 1:i + 1 + h]
                if len(win) < h:                                   # 만기 전
                    continue
                r_s = (1 + rets.loc[win, c['stock_code']]).prod() - 1
                row[f'ret_{h}d'] = r_s
                if c.get('market') in index.columns:
                    row[f'ar_{h}d'] = r_s - ((1 + index.loc[win, c['market']].fillna(0)).prod() - 1)
        out.append(row)
    return pd.DataFrame(out)


def summarize(led, horizons=HORIZONS):
    res = {}
    for h in horizons:
        col = f'ar_{h}d'
        if col not in led:
            res[h] = {'matured': 0}
            continue
        g = led.dropna(subset=[col]).groupby('verdict')[col]
        res[h] = {v: {'n': int(s.size), 'ar_mean': float(s.mean()), 'ar_median': float(s.median()),
                      'share_negative': float((s < 0).mean())} for v, s in g}
    return res


def run_days(live=LIVE):
    out = {}
    for f in sorted(live.glob('2*/run_status.json')):
        s = json.loads(f.read_text(encoding='utf-8'))
        out[s.get('date', f.parent.name)] = s.get('result')
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', type=Path, default=LIVE)
    args = ap.parse_args(argv)
    prices, index = latest_prices()
    cases = load_cases()
    if cases.empty:
        print('실시간 판정 파일이 없다')
        return 0
    led = ledger(cases, load_returns(prices), load_index(index))
    led['after_freeze'] = led['drop_date'] > FROZEN_ON
    args.out.mkdir(parents=True, exist_ok=True)
    led.to_csv(args.out / 'forward_ledger.csv', index=False, encoding='utf-8-sig')
    days = run_days()
    summary = {'prices': prices.name, 'price_last_day': prices.stem.split('_')[-1], 'cases': int(len(led)),
               'verdicts': led['verdict'].value_counts().to_dict(),
               'run_days': {r: sorted(d for d, x in days.items() if x == r) for r in sorted(set(days.values()), key=str)},
               'by_horizon_trading_days': summarize(led),
               'by_horizon_after_freeze': summarize(led[led['after_freeze']])}
    (args.out / 'forward_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
