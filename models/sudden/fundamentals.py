"""사후 펀더멘털 검증 (설계서 7절 보완): 급락 뒤 실제로 주당 이익이 줄었는지로 판정을 확인한다.

가치훼손(H1·H2)은 정의상 미래 현금흐름·주주 몫이 줄어든다는 뜻이다. 사후 주가와 달리 시장 심리·추세의 영향을 덜 받는다.

[자료] OpenDART 단일회사 주요계정(fnlttSinglAcnt.json). 연결(CFS)이 있으면 연결, 없으면 별도(OFS).
      분기 보고서 값은 누적이라 분기값으로 바꾼다: Q1 = 1분기 누적, Q2 = 반기 - Q1, Q3 = 3분기 누적 - 반기, Q4 = 연간 - 3분기 누적.
      발행주식수는 KRX 시세 파일의 상장주식수(LIST_SHRS)를 쓴다.
[지표] H1: dOP_mcap = (TTM_after - TTM_before) / 급락 전 시가총액,  H2: dilution = 주식수_after / 주식수_before - 1
      (보조) 주당 영업이익 변화율 dEPS_op = (TTM_after / 주식수_after - TTM_before / 주식수_before) / 주가_(D-1)
      - TTM_before: D가 속한 분기 직전까지 4개 분기 영업이익 합 (급락 전 실적)
      - TTM_after : D가 속한 분기 다음부터 4개 분기 영업이익 합 (급락 뒤 실적)
      - 주가로 나눠 "시가 대비 이익이 얼마나 바뀌었나"(이익수익률 변화)로 만든다. 증자로 주식수가 늘면 주당 값이 줄어 H2도 반영된다.
      가치훼손 판정은 비가치 판정보다 dEPS_op가 낮아야 한다.

    python models/sudden/fundamentals.py --sample 2015       # output / naver_full
    python models/sudden/fundamentals.py --sample 2023       # output_2023 / naver_2023 (튜닝용만)
"""
import argparse
import csv
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

import backtest as bt
import dart

HERE = Path(__file__).resolve().parent
CACHE = HERE / 'cache/dart/fnltt'
REPORTS = {'11013': 1, '11012': 2, '11014': 3, '11011': 4}       # 1분기, 반기, 3분기, 사업보고서
SAMPLES = {
    '2015': {'out': 'output', 'ach': 'cache/naver_full/ach', 'prices': None, 'split': None},
    '2023': {'out': 'output_2023', 'ach': 'cache/naver_2023/ach', 'split': 'tune',
             'prices': 'cache/krx/prices_20220601_20251231.csv'},
}


def fetch(key, corp_code, year, report, opener=dart.urlopen, pause=0.3):
    path = CACHE / f'{corp_code}_{year}_{report}.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    data = json.loads(dart.get('fnlttSinglAcnt.json', {'corp_code': corp_code, 'bsns_year': year, 'reprt_code': report},
                               key, opener))
    if data.get('status') not in ('000', '013'):
        raise RuntimeError(f"OpenDART 오류 {data.get('status')}: {data.get('message')}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    time.sleep(pause)
    return data


def amount(text):
    try:
        return float(str(text).replace(',', ''))
    except (TypeError, ValueError):
        return None


def cumulative_op(data):
    """보고서 하나에서 영업이익 누적값(연결 우선). 1분기·반기·3분기는 thstrm_add_amount(누적)가 있으면 그 값."""
    rows = [r for r in data.get('list', []) if r.get('account_nm') == '영업이익']
    for fs in ('CFS', 'OFS'):
        for r in rows:
            if r.get('fs_div') == fs:
                return amount(r.get('thstrm_add_amount')) if amount(r.get('thstrm_add_amount')) is not None else amount(r.get('thstrm_amount'))
    return None


def quarterly(cum_by_q):
    """{1: Q1 누적, 2: 반기 누적, 3: 3분기 누적, 4: 연간} → {분기: 분기값}. 빠진 누적값이 있으면 그 분기는 None."""
    out, prev = {}, 0.0
    for q in (1, 2, 3, 4):
        c = cum_by_q.get(q)
        out[q] = None if c is None or prev is None else c - prev
        prev = c
    return out


def quarter_of(day):
    d = pd.Timestamp(day)
    return d.year, (d.month - 1) // 3 + 1


def shift(year, q, k):
    i = year * 4 + (q - 1) + k
    return i // 4, i % 4 + 1


def ttm(series, year, q, before=True):
    """before: (year,q) 직전 4개 분기. after: (year,q) 다음 4개 분기. 하나라도 없으면 None."""
    keys = [shift(year, q, -k) for k in range(1, 5)] if before else [shift(year, q, k) for k in range(1, 5)]
    vals = [series.get(k) for k in keys]
    return None if any(v is None for v in vals) else sum(vals), keys


def shares_and_price(prices, code, day, after_end=None):
    g = prices[prices['stock_code'] == code].sort_values('trade_date')
    before = g[g['trade_date'] < pd.Timestamp(day)]
    if before.empty:
        return None
    row = before.iloc[-1]
    out = {'price_prev': row['close'], 'shares_before': row['shares']}
    if after_end is not None:
        later = g[g['trade_date'] <= pd.Timestamp(after_end)]
        out['shares_after'] = later.iloc[-1]['shares'] if len(later) else None
    return out


def load_prices(path):
    raw = pd.read_csv(path, dtype=str, encoding='utf-8-sig')
    raw = raw[raw['BAS_DD'].str.fullmatch(r'\d{8}', na=False)]
    num = lambda s: pd.to_numeric(s.str.replace(',', ''), errors='coerce')
    return pd.DataFrame({'stock_code': raw['ISU_CD'].str.strip(), 'trade_date': pd.to_datetime(raw['BAS_DD'], format='%Y%m%d'),
                         'close': num(raw['TDD_CLSPRC']), 'shares': num(raw['LIST_SHRS'])})


def event_metric(series, px, day):
    year, q = quarter_of(day)
    before, _ = ttm(series, year, q, True)
    after, keys = ttm(series, year, q, False)
    if before is None or after is None or not px or not px.get('price_prev'):
        return None
    end_y, end_q = keys[-1]
    sb, sa = px['shares_before'], px.get('shares_after') or px['shares_before']
    if not sb or not sa:
        return None
    mcap = px['price_prev'] * sb
    return {'ttm_before': before, 'ttm_after': after, 'shares_before': sb, 'shares_after': sa, 'price_prev': px['price_prev'],
            'dEPS_op': (after / sa - before / sb) / px['price_prev'],
            'dOP_mcap': (after - before) / mcap,             # H1: 영업이익 변화 / 급락 전 시가총액
            'dilution': sa / sb - 1,                          # H2: 주식 수 증가율
            'after_end_quarter': f'{end_y}Q{end_q}'}


def run(sample):
    cfg = SAMPLES[sample]
    out = HERE / cfg['out']
    with (out / 'drop_events.csv').open(encoding='utf-8-sig', newline='') as f:
        events = [e for e in csv.DictReader(f) if not cfg['split'] or e.get('split') == cfg['split']]
    key = dart.api_key()
    codes = dart.corp_codes(key)
    import events as ev
    prices = load_prices(HERE / cfg['prices'] if cfg['prices'] else ev.T.PRICE_PATH)
    series_by_code = {}
    for code in sorted({e['stock_code'] for e in events}):
        corp = codes.get(code)
        if not corp:
            continue
        years = range(min(int(e['D'][:4]) for e in events if e['stock_code'] == code) - 1,
                      max(int(e['D'][:4]) for e in events if e['stock_code'] == code) + 3)
        series = {}
        for y in years:
            cum = {}
            for rep, qn in REPORTS.items():
                cum[qn] = cumulative_op(fetch(key, corp, y, rep))
            for qn, v in quarterly(cum).items():
                series[(y, qn)] = v
        series_by_code[code] = series
    rows = []
    for e in events:
        series = series_by_code.get(e['stock_code'])
        if series is None:
            continue
        year, q = quarter_of(e['D'])
        end_y, end_q = shift(year, q, 4)
        after_end = pd.Timestamp(year=end_y, month=end_q * 3, day=1) + pd.offsets.MonthEnd(0)
        m = event_metric(series, shares_and_price(prices, e['stock_code'], e['D'], after_end), e['D'])
        rows.append({'drop_id': e['drop_id'], 'stock_code': e['stock_code'], 'D': e['D'],
                     **(m or {'dEPS_op': None, 'dOP_mcap': None, 'dilution': None})})
    return rows


def compare(rows, ach_dir, labels=None):
    """labels: {drop_id: 판정}. 없으면 ach 결과의 판정을 쓴다."""
    if labels is None:
        labels = {}
        for f in Path(ach_dir).glob('[0-9]*_[0-9]*.json'):
            r = json.loads(f.read_text(encoding='utf-8'))
            labels[r['drop_id']] = r['verdict']
    data = [{**r, 'verdict': labels.get(r['drop_id'], '판단보류')} for r in rows if r['drop_id'] in labels]
    clip = lambda v: None if v is None else max(-0.5, min(0.5, v))   # 소형 적자주의 극단값이 평균을 흔들지 않게 ±50%에서 자른다
    data = [{**r, 'dOP_mcap': clip(r.get('dOP_mcap')), 'dEPS_op': clip(r.get('dEPS_op'))} for r in data]
    # 기대: 영업이익 변화는 비가치 - 가치훼손 > 0 (가치훼손이 더 줄었다), 희석률은 < 0 (가치훼손이 더 늘었다)
    return {'n_with_metric': sum(r.get('dOP_mcap') is not None for r in data),
            'dOP_mcap': bt.summarize(data, 'dOP_mcap'), 'dilution': bt.summarize(data, 'dilution'),
            'dEPS_op': bt.summarize(data, 'dEPS_op'),
            'note': 'dEPS_op는 적자 기업이 증자하면 주당 적자가 줄어 좋아진 것처럼 보여 보조로만 쓴다'}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sample', choices=sorted(SAMPLES), default='2015')
    args = ap.parse_args(argv)
    rows = run(args.sample)
    ach_dir = HERE / SAMPLES[args.sample]['ach']
    with (ach_dir / 'fundamentals.csv').open('w', encoding='utf-8-sig', newline='') as f:
        cols = ['drop_id', 'stock_code', 'D', 'ttm_before', 'ttm_after', 'shares_before', 'shares_after', 'price_prev',
                'dOP_mcap', 'dilution', 'dEPS_op', 'after_end_quarter']
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    result = compare(rows, ach_dir)
    (ach_dir / 'fundamentals.json').write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
