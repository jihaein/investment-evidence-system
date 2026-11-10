"""OpenDART 공시 목록으로 3차 ACH의 필수 공시 단위를 만든다 (설계서 v3.1 3.1·5.5절).

필수 공시는 1·2차를 거치지 않고 3차로 바로 간다. 원문 XML은 파싱하지 않고 공시 제목(report_nm)만 쓴다(설계서 6절).
제목만으로 유형·방향이 정해지는 공시만 단위로 만들고, 실적·지분 보고처럼 방향을 알려면 본문 숫자가 필요한 공시는
needs_detail로 기록만 한다. 등급은 사건성 공시 1.0, 지분 보고 공시 0.6이다.

[시점] OpenDART는 접수일자만 준다. 그날 세션(전 거래일 15:30 ~ 그날 15:30)에 배정하고 time_unknown으로 표시한다.
     급락일 당일 공시는 t0 뒤에 나왔을 수 있어 on_drop_day로 표시한다(민감도 확인용).

[키] 저장소 맨 위 .env에 OPENDART_API_KEY=... (https://opendart.fss.or.kr 에서 무료 발급).

[실행]
    python models/sudden/dart.py            # 212건 사건 창의 공시 목록 수집 → cache/dart/
"""
import argparse
import csv
import io
import json
import os
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
OUT = HERE / os.getenv('SUDDEN_OUTPUT', 'output')     # 다른 표본(예: output_2023)을 쓸 때 바꾼다
ROOT = HERE.parents[1]
BASE = 'https://opendart.fss.or.kr/api'
CACHE = HERE / 'cache/dart'
USER_AGENT = 'investment-evidence-system research crawler'

# (제목 패턴, 유형, 방향, 등급, 종류). 위에서부터 처음 맞는 규칙을 쓴다.
SKIP = re.compile(r'종속회사의\s*주요경영사항|자회사의\s*주요경영사항|거래정지\s*해제|미확정\)|정정명령')
RULES = [
    (r'자기주식.*(?:신탁계약\s*해지|처분)|신탁계약\s*해지', 'C4', '부정', 1.0, 'event'),
    (r'공급계약\s*해지|공급계약\s*취소', 'F2', '부정', 1.0, 'event'),
    (r'단일판매.*공급계약\s*체결|공급계약\s*체결', 'F2', '긍정', 1.0, 'event'),
    (r'유상증자\s*결정', 'C1', '부정', 1.0, 'event'),
    (r'전환사채권?\s*발행\s*결정|신주인수권부사채권?\s*발행\s*결정|교환사채권?\s*발행\s*결정', 'C1', '부정', 1.0, 'event'),
    (r'감사의견\s*(?:거절|부적정|한정)|의견거절|감사범위\s*제한', 'C2', '부정', 1.0, 'event'),
    (r'상장폐지|관리종목\s*지정|거래정지|불성실공시법인\s*지정', 'C2', '부정', 1.0, 'event'),
    (r'횡령|배임', 'C3', '부정', 1.0, 'event'),
    (r'경영권\s*분쟁|소송.*(?:주주총회|경영권)', 'C3', '부정', 1.0, 'event'),
    (r'소송\s*등의\s*(?:제기|신청)', 'F5', '부정', 1.0, 'event'),
    (r'풍문\s*또는\s*보도에\s*대한\s*해명|조회공시\s*요구.*답변\s*\((?:중요정보\s*없음|부인)\)|해명', 'R2', '없음', 1.0, 'event'),
]
NEEDS_DETAIL = re.compile(r'영업\(잠정\)실적|잠정실적|매출액또는손익구조|최대주주\s*변경|주식등의대량보유|'
                          r'임원ㆍ주요주주|임원·주요주주|감사보고서\s*제출')
SHARE_REPORT = re.compile(r'주식등의대량보유|임원ㆍ주요주주|임원·주요주주')


def api_key():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
    except ImportError:
        pass
    return os.getenv('OPENDART_API_KEY') or os.getenv('DART_API_KEY')


def classify(report_nm):
    """공시 제목 → 단위 정보. 방향을 알 수 없는 필수 공시는 needs_detail, 그 밖의 공시는 None."""
    raw = ' '.join((report_nm or '').split())                          # 끝에 붙은 공백 정리
    name = re.sub(r'^\s*\[[^\]]*\]\s*', '', raw)                        # [기재정정] 등 머리표 제거
    correction = name != raw
    if SKIP.search(name):                                  # 종속회사 공시, 거래정지 해제, 미확정 답변은 판정 정보가 아니다
        return None
    for pattern, unit_type, direction, grade, kind in RULES:
        if re.search(pattern, name):
            return {'type': unit_type, 'direction': direction, 'grade': grade, 'kind': kind,
                    'report': name, 'correction': correction}
    if NEEDS_DETAIL.search(name):
        return {'type': None, 'direction': None, 'grade': 0.6 if SHARE_REPORT.search(name) else 1.0,
                'kind': 'needs_detail', 'report': name, 'correction': correction}
    return None


def get(path, params, key, opener=urlopen):
    req = Request(f'{BASE}/{path}?{urlencode({"crtfc_key": key, **params})}', headers={'User-Agent': USER_AGENT})
    with opener(req, timeout=30) as resp:
        return resp.read()


def corp_codes(key, opener=urlopen, cache=CACHE):
    path = cache / 'corp_codes.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    raw = get('corpCode.xml', {}, key, opener)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        root = ET.fromstring(z.read(z.namelist()[0]))
    listed = [e for e in root.iter('list') if (e.findtext('stock_code') or '').strip()]
    codes = {e.findtext('stock_code').strip(): e.findtext('corp_code').strip() for e in listed}
    names = {e.findtext('stock_code').strip(): (e.findtext('corp_name') or '').strip() for e in listed}
    cache.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(codes, ensure_ascii=False), encoding='utf-8')
    (cache / 'corp_names.json').write_text(json.dumps(names, ensure_ascii=False), encoding='utf-8')
    return codes


def corp_names(key, opener=urlopen, cache=CACHE):
    """종목코드 → DART 공식 회사명(현재 이름). KRX 약칭은 잘린 경우가 있어(예: 현대엘리베이) 검색어 보정에 쓴다."""
    path = cache / 'corp_names.json'
    if not path.exists():
        (cache / 'corp_codes.json').unlink(missing_ok=True)
        corp_codes(key, opener, cache)
    return json.loads(path.read_text(encoding='utf-8'))


def filings(key, corp_code, bgn, end, opener=urlopen, cache=CACHE, pause=0.3):
    """bgn·end: YYYYMMDD. 페이지를 끝까지 받고 결과를 캐시에 저장한다."""
    path = cache / 'list' / f'{corp_code}_{bgn}_{end}.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    out, page = [], 1
    while True:
        data = json.loads(get('list.json', {'corp_code': corp_code, 'bgn_de': bgn, 'end_de': end,
                                            'page_no': page, 'page_count': 100}, key, opener))
        if data.get('status') == '013':                      # 조회된 데이터 없음
            break
        if data.get('status') != '000':
            raise RuntimeError(f"OpenDART 오류 {data.get('status')}: {data.get('message')}")
        out += data.get('list', [])
        if page >= int(data.get('total_page', 1)):
            break
        page += 1
        time.sleep(pause)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    return out


def window_units(rows, sessions, company):
    """사건 창 세션(d=0,1,2)의 날짜에 접수된 공시 → 필수 공시 단위와 needs_detail 목록."""
    by_date = {s['trade_date'].replace('-', ''): int(s['d']) for s in sessions}
    units, detail = [], []
    for r in rows:
        d = by_date.get(r.get('rcept_dt'))
        if d is None:
            continue
        c = classify(r.get('report_nm'))
        if not c:
            continue
        info = {'rcept_no': r.get('rcept_no'), 'rcept_dt': r['rcept_dt'], 'session_d': d, 'time_unknown': True,
                'on_drop_day': d == 0, 'report': c['report'], 'correction': c['correction']}
        if c['kind'] == 'needs_detail':
            detail.append({**info, 'grade': c['grade']})
            continue
        units.append({**info, 'type': c['type'], 'direction': c['direction'], 'grade': c['grade'],
                      'quote': c['report'], 'event': f"{company}-{c['report']}", 'mandatory': True,
                      'url': f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={r.get('rcept_no')}"})
    return units, detail


def collect(events, sessions, key, opener=urlopen, cache=CACHE):
    codes = corp_codes(key, opener, cache)
    out = {}
    for e in events:
        ss = sessions.get(e['drop_id'], [])
        corp = codes.get(e['stock_code'])
        if not ss or not corp:
            out[e['drop_id']] = {'status': 'no_corp_code' if not corp else 'no_sessions', 'units': [], 'needs_detail': []}
            continue
        dates = sorted(s['trade_date'].replace('-', '') for s in ss)
        rows = filings(key, corp, dates[0], dates[-1], opener, cache)
        units, detail = window_units(rows, ss, e['company_name'])
        out[e['drop_id']] = {'status': 'ok', 'corp_code': corp, 'filings': len(rows), 'units': units, 'needs_detail': detail}
    return out


def units_path(cache=CACHE):
    """표본마다 따로 둔다: 기본 표본은 event_units.json, 다른 표본은 event_units_<폴더 이름>.json."""
    return cache / ('event_units.json' if OUT.name == 'output' else f'event_units_{OUT.name}.json')


def load_units(cache=CACHE):
    path = units_path(cache)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args(argv)
    key = api_key()
    if not key:
        raise SystemExit('OPENDART_API_KEY가 없습니다. https://opendart.fss.or.kr 에서 발급받아 저장소 맨 위 .env 에 넣어 주세요.')
    with (OUT / 'drop_events.csv').open(encoding='utf-8-sig', newline='') as f:
        events = list(csv.DictReader(f))
    sessions = {}
    with (OUT / 'drop_sessions.csv').open(encoding='utf-8-sig', newline='') as f:
        for s in csv.DictReader(f):
            sessions.setdefault(s['drop_id'], []).append(s)
    out = collect(events, sessions, key)
    CACHE.mkdir(parents=True, exist_ok=True)
    units_path().write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    n_units = sum(len(v['units']) for v in out.values())
    print(json.dumps({'events': len(out), 'events_with_units': sum(bool(v['units']) for v in out.values()),
                      'units': n_units, 'needs_detail': sum(len(v['needs_detail']) for v in out.values())},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
