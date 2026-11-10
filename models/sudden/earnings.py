"""실적 공시 → 방향 있는 F1 단위 (설계서 5.5절 보완).

잠정실적·손익구조 변동 공시는 제목만으로 좋은지 나쁜지 알 수 없어 dart.py가 needs_detail로만 남겼다.
OpenDART 분기 재무(fnlttSinglAcnt, fundamentals.py와 같은 캐시)로 보고 기간의 영업이익을 전년 같은 기간과 비교해 방향을 붙인다.

[보고 기간] 접수일 기준
    잠정실적: 1~3월 → 전년 4분기, 4~6월 → 1분기, 7~9월 → 2분기, 10~12월 → 3분기
    손익구조 30% 변동: 1~4월 접수만 쓰고 전년 연간 (그 밖은 반기 등이라 건너뛴다)
[방향] 흑자→적자 = 부정, 적자→흑자 = 긍정, 그 밖은 변화율 (cur - prev) / |prev| 이 -10% 이하 부정, +10% 이상 긍정.
      ±10% 안이거나 숫자가 없으면 단위를 만들지 않고 earnings_skipped로 기록한다.
[한계] 공시 당시의 잠정치가 아니라 나중 정기보고서 값을 쓴다(대부분 같다). 컨센서스 대비가 아니라 전년 대비다.

    python models/sudden/earnings.py                              # output 표본 (cache/dart/event_units.json)
    SUDDEN_OUTPUT=output_2023 python models/sudden/earnings.py    # 2023 표본
"""
import argparse
import json
import re

import dart
import fundamentals as fu

EARNINGS = re.compile(r'잠정\)?실적|잠정실적')
PROFIT_CHANGE = re.compile(r'매출액또는손익구조')
THRESHOLD = 0.10


def reported_period(rcept_dt, report):
    """('Q', year, q) 또는 ('Y', year, None). 알 수 없으면 None."""
    y, m = int(rcept_dt[:4]), int(rcept_dt[4:6])
    if EARNINGS.search(report):
        return ('Q', y - 1, 4) if m <= 3 else ('Q', y, (m - 1) // 3)
    if PROFIT_CHANGE.search(report):
        return ('Y', y - 1, None) if m <= 4 else None
    return None


def direction(cur, prev, threshold=THRESHOLD):
    if cur is None or prev is None:
        return None, None
    if prev > 0 >= cur:
        return '부정', None
    if prev <= 0 < cur:
        return '긍정', None
    if prev == 0:
        return None, None
    change = (cur - prev) / abs(prev)
    if change <= -threshold:
        return '부정', change
    if change >= threshold:
        return '긍정', change
    return None, change


def period_values(fetch_year, period):
    """fetch_year(year) → {1: Q1 누적, 2: 반기, 3: 3분기, 4: 연간}. 보고 기간과 전년 같은 기간의 영업이익."""
    kind, year, q = period
    if kind == 'Y':
        return fetch_year(year).get(4), fetch_year(year - 1).get(4)
    return fu.quarterly(fetch_year(year)).get(q), fu.quarterly(fetch_year(year - 1)).get(q)


def describe(period, cur, prev, change):
    label = f'{period[1]}년' if period[0] == 'Y' else f'{period[1]}년 {period[2]}분기'
    if change is None:
        trend = '적자 전환' if cur <= 0 else '흑자 전환'
    else:
        trend = f'전년 대비 {change:+.0%}'
    return f'{label} 영업이익 {cur / 1e8:,.0f}억원, {trend}'


def event_units(detail, fetch_year, company):
    """needs_detail 목록 중 실적 공시 → (F1 단위, 건너뛴 기록). 같은 보고 기간은 한 단위로 합친다."""
    units, skipped, seen = [], [], set()
    for d in sorted(detail, key=lambda x: (x['session_d'], x['rcept_no'])):
        period = reported_period(d['rcept_dt'], d['report'])
        if period is None:
            if EARNINGS.search(d['report']) or PROFIT_CHANGE.search(d['report']):
                skipped.append({**d, 'why': 'period_unknown'})
            continue
        if period in seen:
            continue
        seen.add(period)
        cur, prev = period_values(fetch_year, period)
        dirn, change = direction(cur, prev)
        if dirn is None:
            skipped.append({**d, 'why': 'no_numbers' if cur is None or prev is None else 'small_change',
                            'op_cur': cur, 'op_prev': prev})
            continue
        quote = describe(period, cur, prev, change)
        units.append({**{k: d[k] for k in ('rcept_no', 'rcept_dt', 'session_d', 'time_unknown', 'on_drop_day', 'report', 'correction')},
                      'type': 'F1', 'direction': dirn, 'grade': 1.0, 'quote': quote, 'event': f'{company}-{d["report"]}',
                      'mandatory': True, 'source': 'earnings_yoy', 'op_cur': cur, 'op_prev': prev, 'yoy': change,
                      'url': f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={d['rcept_no']}"})
    return units, skipped


def enrich(by_event, names, key, fetcher=fu.fetch):
    """event_units 결과에 F1 단위를 더한다. 다시 돌려도 같은 결과가 되게 전에 붙인 실적 단위는 지운다."""
    stats = {'events': 0, 'units': 0, '부정': 0, '긍정': 0, 'skipped': 0}
    for drop_id, rec in by_event.items():
        rec['units'] = [u for u in rec.get('units', []) if u.get('source') != 'earnings_yoy']
        rec.pop('earnings_skipped', None)
        corp = rec.get('corp_code')
        if not corp or not rec.get('needs_detail'):
            continue
        cache = {}

        def fetch_year(year):
            if year not in cache:
                cache[year] = {qn: fu.cumulative_op(fetcher(key, corp, year, rep)) for rep, qn in fu.REPORTS.items()}
            return cache[year]
        units, skipped = event_units(rec['needs_detail'], fetch_year, names.get(drop_id, ''))
        rec['units'] += units
        if skipped:
            rec['earnings_skipped'] = skipped
        stats['events'] += bool(units)
        stats['units'] += len(units)
        stats['skipped'] += len(skipped)
        for u in units:
            stats[u['direction']] += 1
    return stats


def main(argv=None):
    import csv
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args(argv)
    key = dart.api_key()
    if not key:
        raise SystemExit('OPENDART_API_KEY가 없습니다 (.env).')
    path = dart.units_path()
    by_event = json.loads(path.read_text(encoding='utf-8'))
    with (dart.OUT / 'drop_events.csv').open(encoding='utf-8-sig', newline='') as f:
        names = {e['drop_id']: e['company_name'] for e in csv.DictReader(f)}
    stats = enrich(by_event, names, key)
    path.write_text(json.dumps(by_event, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({'file': path.name, **stats}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
