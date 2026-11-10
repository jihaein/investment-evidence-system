"""3차 ACH 판정 (설계서 v3.1 5절): LLM은 정보 단위 분류만 하고, 판정은 고정된 관계표로 코드가 계산한다.

흐름: 사건 창 묶음 → 선정 점수 S_sel로 최대 12건 선정 → LLM이 정보 단위로 나눠 유형·방향·사건 이름 분류(3회 다수결)
     → 인용 확인·확실성(r_cert) 규칙 → 2단계 병합 → 유형별 가중치 집계 → 가설별 지지·반증 → 판정·플래그

[임시 대응] 설계서 입력 중 아직 없는 것
- S_sel = 100·V·(0.55N + 0.25P + 0.20T). V는 1로 두고, N·P는 사실 기반 N·P(np_scores, 합산 P)를 쓴다.
  N·P가 보류된 묶음은 0.5로 채우고 n_missing/p_missing으로 표시한다. 이슈 군집당 3개 제한은 아직 없다.
- r_src는 같은 묶음에 6시간 안에 언론사 3곳 이상 + 대표 기사에 "밝혔다"가 있으면 보도자료(0.8)로 본다.
- DART 필수 공시, 시장 규칙 K1~K4, 업종 트랙은 아직 없다. M1은 그날 시장지수가 -1.5% 이하일 때만 유효하다.
- 병합 유사도는 임베딩(BGE-M3) 대신 문자열 유사도(difflib)를 쓴다.
- gpt-5 계열은 temperature 0을 받지 않아 기본값으로 3회 실행해 다수결을 낸다.

[실행]
    python models/sudden/ach.py --cache models/sudden/cache/naver_full            # 212건 전체
    python models/sudden/ach.py --drop-id 010170_20170817                          # 한 건
"""
import argparse
import csv
import hashlib
import json
import os
import random
import re
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import llm_facts as lf
import news_review as review

HERE = Path(__file__).resolve().parent
OUT = HERE / os.getenv('SUDDEN_OUTPUT', 'output')     # 다른 표본(예: output_2023)을 쓸 때 바꾼다
PROMPT_VERSION = 'ach-units-3'
RULE_VERSION = 'ach-v3.1-provisional-2'
RUNS = 3
MAX_CHARS = 4000

TYPES = {
    'F1': ('실적·전망 하향', '잠정실적 쇼크, 가이던스 하향'),
    'F2': ('계약·수주 변동', '공급계약 해지·축소, 대형 수주'),
    'F3': ('고객·수요 변화', '핵심 고객 이탈, 주력 제품 수요 둔화'),
    'F4': ('원가·마진', '원재료 가격 상승, 판가 인하'),
    'F5': ('규제·법률', '이 회사에 대한 제재, 인허가 취소, 소송 패소'),
    'F6': ('사업 사고·실패', '공장 사고, 리콜, 임상·기술 실패'),
    'F7': ('경쟁 구도 변화', '경쟁사 신제품, 점유율 하락'),
    'C1': ('지분 희석', '유상증자, CB·BW 발행 결정'),
    'C2': ('재무 건전성', '신용등급 하향, 유동성 위기, 감사의견'),
    'C3': ('지배구조·경영진 리스크', '횡령·배임, 경영권 분쟁'),
    'C4': ('주주환원 변경', '배당 축소, 자사주 정책 철회'),
    'M1': ('거시·시장 전반 충격', '금리 급변, 환율 급등, 지수 급락'),
    'M2': ('업종 전망 악화', '업황 둔화, 업종 수요 감소, 업종 규제 강화'),
    'M3': ('업종 심리·연쇄 하락', '테마 차익실현, 동종사 급락에 따른 동반 매도(펀더멘털 언급 없음)'),
    'S1': ('내부자·대주주 매도', '최대주주·임원 지분 매도'),
    'S2': ('블록딜·오버행', '시간외 대량매매, 보호예수 해제, CB 전환 물량'),
    'S3': ('신용·반대매매', '신용잔고 급증, 반대매매'),
    'S4': ('지수·패시브 수급', '지수 편출, 리밸런싱'),
    'R1': ('애널리스트 의견 변경', '목표가·투자의견 하향'),
    'R2': ('해명·반박·정정', '"사실무근" 조회공시 답변, 회사 해명, 오보 정정'),
    'X0': ('판정 정보 없음', '주가 움직임 서술, 홍보, 행사, 인사'),
}
HYPOTHESES = ('H1', 'H2', 'H3', 'H4', 'H5')
GROUP = {'H1': '가치훼손', 'H2': '가치훼손', 'H3': '비가치', 'H4': '비가치', 'H5': '비가치', 'H0': '판단보류'}
# 설계서 5.4 관계표(부정 방향 기준): H1 H2 H3 H4 H5
_REL = {
    ('F1', 'F2', 'F3', 'F5', 'F6'): '+0-0-', ('F4',): '+000-', ('F7',): '+0000', ('C1',): '0+-0-',
    ('C2', 'C3', 'C4'): '++-0-', ('M1',): '00+00', ('M2',): '+0-0-', ('M3',): '00+00', ('S1',): '+00+0',
    ('S2', 'S4'): '000+0', ('S3',): '00++0', ('R1',): '++000', ('R2',): '--00+',
    ('K1a',): '0-+00', ('K1b',): '00-00', ('K4',): '00++0', ('K3',): '000+0',     # 시장 규칙(아직 만들지 않음)
}
RELATION = {t: dict(zip(HYPOTHESES, v)) for ts, v in _REL.items() for t in ts}
NO_DIRECTION = {'R2', 'K1a', 'K1b', 'K3', 'K4'}                                    # 방향 개념이 없어 긍정이어도 뒤집지 않는다

EXAMPLES = {                                             # 설계서 5.2: 유형마다 예시 2개
    'F1': ('A사의 3분기 영업이익은 전년 대비 62% 줄었다', '증권사는 A사의 올해 영업이익 전망치를 20% 낮췄다'),
    'F2': ('A사는 B사와 맺은 120억원 규모 공급계약을 해지했다고 공시했다', 'A사가 300억원 규모 장비를 수주했다'),
    'F3': ('A사의 최대 고객사인 B사가 거래를 중단했다', 'A사 주력 제품의 주문이 2분기부터 급감했다'),
    'F4': ('니켈 가격 급등으로 A사의 원가 부담이 커졌다', 'A사는 주력 제품 판매가를 15% 인하했다'),
    'F5': ('공정위는 A사에 과징금 50억원을 부과했다', 'A사는 특허 소송 1심에서 패소했다'),
    'F6': ('A사 공장에서 화재가 발생해 가동이 중단됐다', 'A사의 신약 임상 3상이 1차 지표 달성에 실패했다'),
    'F7': ('경쟁사 B사가 같은 기능의 저가 제품을 출시했다', 'A사의 국내 점유율이 30%에서 22%로 떨어졌다'),
    'C1': ('A사는 운영자금 200억원 마련을 위해 유상증자를 결정했다', 'A사는 300억원 규모 전환사채 발행을 결정했다'),
    'C2': ('A사는 감사의견 거절로 관리종목에 지정됐다', 'A사는 자회사에 대한 채무보증 500억원을 결정했다'),
    'C3': ('A사 전 대표가 횡령 혐의로 기소됐다', '2대 주주가 경영권 확보를 위해 임시주총 소집을 청구했다'),
    'C4': ('A사는 올해 배당을 하지 않기로 했다', 'A사는 예정된 자사주 매입 계획을 철회했다'),
    'M1': ('미국 금리 인상 우려로 코스닥 지수가 4% 급락했다', '원·달러 환율이 하루 새 30원 올랐다'),
    'M2': ('반도체 업황 둔화로 장비 업체들의 수주가 줄어들 전망이다', '정부가 게임 업종 규제 강화안을 발표했다'),
    'M3': ('바이오주 전반에 차익실현 매물이 쏟아졌다', '동종사 B사 급락에 A사도 동반 하락했다'),
    'S1': ('A사 최대주주가 보유 지분 5%를 장내 매도했다', 'A사 임원이 보유 주식 10만주를 팔았다'),
    'S2': ('A사 주식 200만주가 시간외 대량매매(블록딜)로 거래됐다', 'A사 전환사채 50만주의 전환청구권이 행사돼 상장된다'),
    'S3': ('A사의 신용잔고율이 10%를 넘었다', '주가 하락으로 반대매매 물량이 쏟아졌다'),
    'S4': ('A사가 코스피200 지수에서 편출된다', '지수 정기 변경에 따른 패시브 자금 매도가 예상된다'),
    'R1': ('B증권은 A사의 목표주가를 3만원에서 2만원으로 낮췄다', 'C증권은 A사 투자의견을 매수에서 중립으로 내렸다'),
    'R2': ('A사는 매각설에 대해 "사실무근"이라고 조회공시에 답변했다', 'A사는 보도된 리콜 규모가 사실과 다르다고 해명했다'),
    'X0': ('A사 주가는 장중 12% 하락했다', 'A사는 1998년 설립된 반도체 장비 업체로 매출의 70%가 수출이다'),
}

SYSTEM = """너는 한국 경제 기사를 정보 단위로 나누고 분류하는 도구다. 판정이나 원인 추론은 하지 않는다.
[단위 나누기]
- 정보 단위는 이번 기사가 새로 알리는 하나의 사건·사실, 또는 이번에 새로 나온 명시적 전망 변경이다.
- 한 문장에서 단위는 최대 하나다. 같은 사실을 다른 문장이 다시 말하면 처음 나온 곳 하나만 낸다.
- 회사 소개, 사업 현황, 과거 실적 수치의 나열, 시장 규모, 배경 설명, 제품 설명은 단위로 내지 않는다(X0도 내지 않는다).
- 기사 전체에 판정 정보가 없으면 X0 단위 하나만 낸다.
- 주어진 기사에 적힌 내용만 쓴다. 기사 밖 지식, 나중에 일어난 일, 추측은 쓰지 않는다.
[필드]
- quote: 그 단위의 근거 구절을 기사에서 한 글자도 바꾸지 말고 15~80자 복사한다.
- type: 아래 유형표의 코드 하나.
- direction: 대상 회사에 나쁜 소식이면 부정, 좋은 소식이면 긍정. X0와 R2는 없음.
  긍정은 이 회사의 실적·계약·수요가 실제로 좋아졌거나 명시적으로 상향 전망된 경우만 쓴다.
- event: 단위가 가리키는 사건을 "주체-사건" 형태의 짧은 명사구로 쓴다(예: "A사-B사 공급계약 해지").
[혼동 규칙]
- 이 회사 이름이 주어인 실적 수치·실적 전망은 F1, 특정 계약의 체결·해지·수주는 F2, 특정 계약이 아닌 고객·주문·수요 변화는 F3.
- 업종 전체(이 회사 이름 없이)의 업황·수요·규제 변화는 M2, 이 회사 제품·고객의 수요는 F3.
- 업종 기사에 수요·규제·업황 같은 미래 실적 요인이 있으면 M2, 그런 언급 없이 주가 동반 하락만 다루면 M3.
- 증권사·연구원의 목표가·투자의견 변경은 R1. 목표가 변경 없이 실적 전망만 말하면 F1(하향 부정, 상향 긍정).
  막연한 "수혜 기대", "성장 기대" 같은 표현만 있으면 단위로 내지 않는다.
- 새 증자·CB·BW 발행 결정은 C1이고 항상 부정이다. 이미 발행된 CB·BW의 전환청구·상장, 보호예수 해제, 블록딜은 S2.
- 감사의견, 관리종목 지정, 자본잠식, 채무보증, 자회사 대여금, 담보 제공, 신용등급은 C2.
- 최대주주·임원이 직접 판 지분은 S1. 최대주주 변경 자체는 매도가 확인되면 S1, 경영권 다툼이면 C3, 둘 다 아니면 X0.
- 증자 관련 소송·가처분은 C3. 회사의 사실무근·해명·정정은 R2.
- 이 회사가 아니라 다른 회사의 소식이면, 이 회사에 영향을 준다고 기사에 적힌 경우에만 분류한다.
- 주가 움직임 서술, 홍보, 행사, 인사, 수상은 X0.
[유형표] 코드: 이름 - 예시
""" + '\n'.join(f'- {k}: {n} - {e}. 예: "{EXAMPLES[k][0]}" / "{EXAMPLES[k][1]}"' for k, (n, e) in TYPES.items())

SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['units'], 'properties': {'units': {
    'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
                               'required': ['quote', 'type', 'direction', 'event'],
                               'properties': {'quote': {'type': 'string'}, 'type': {'type': 'string', 'enum': list(TYPES)},
                                              'direction': {'type': 'string', 'enum': ['부정', '긍정', '없음']},
                                              'event': {'type': 'string'}}}}}}

UNVERIFIED = re.compile(r'(?:추진|매각|인수|합병|부도|퇴출|증자|교체|사임|도산)설|루머|풍문|관계자에 따르면|소식통|'
                        r'검토\s?중|알려졌다|전해졌다|업계에 따르면|가능성이 제기')
ESTIMATED = re.compile(r'전망|추정|예상|관측|가능성|우려')
PLANNED = re.compile(r'하기로\s?(?:했|결정|합의)|예정')


def squash(s):
    return ''.join((s or '').split())


def sim(a, b):
    return SequenceMatcher(None, squash(a), squash(b)).ratio()


def certainty(quote, text):
    """인용 구절이 든 문장의 표현으로 확실성을 정한다(설계서 4.4). 미확인 > 추정 > 예정 > 확정 순으로 본다."""
    sentence = next((s for s in re.split(r'(?<=[.!?다])\s+|\n', text) if squash(quote)[:12] in squash(s)), quote)
    if UNVERIFIED.search(sentence):
        return 0.4, '미확인'
    if ESTIMATED.search(sentence):
        return 0.6, '추정'
    if PLANNED.search(sentence):
        return 0.8, '예정'
    return 1.0, '확정'


def session_distance(published_at, sessions):
    t = datetime.fromisoformat(published_at)
    for s in sessions:
        if datetime.fromisoformat(s['session_start']) < t <= datetime.fromisoformat(s['session_end']):
            return int(s['d'])
    return None


def source_reliability(group, body):
    first = datetime.fromisoformat(group['first_published_at'])
    early = {m['publisher'] for m in group['members']
             if m.get('publisher') and datetime.fromisoformat(m['published_at']) <= first + timedelta(hours=6)}
    return (0.8, '보도자료') if len(early) >= 3 and '밝혔다' in body[:600] else (1.0, '일반 취재')


def selection(groups, rows_by_url, sessions):
    """설계서 4.5: S_sel 높은 순, 20점 이상 최대 12개, 5개 미만이면 채우고 low_confidence."""
    docs = []
    for g in groups:
        a = rows_by_url.get(g['representative_url'])
        if not a:
            continue
        n, p = g['N'].get('value'), g['P'].get('value')
        d = session_distance(g['first_published_at'], sessions)
        t = 2 ** -d if d is not None else 0.25
        s_sel = 100 * 1.0 * (0.55 * (0.5 if n is None else n) + 0.25 * (0.5 if p is None else p) + 0.20 * t)
        r_src, src = source_reliability(g, a.get('body', ''))
        docs.append({'bundle_id': g['group_id'], 'url': a['url'], 'title': a.get('title', ''),
                     'text': review.clean_body(a.get('body', ''))[:MAX_CHARS], 'published_at': a['published_at'],
                     'session_d': d, 'S_sel': round(s_sel, 2), 'N': n, 'P': p, 'T': t, 'V': 1.0,
                     'n_missing': n is None, 'p_missing': p is None, 'r_src': r_src, 'source_type': src,
                     'publisher_count': g.get('publisher_count') or len({m['publisher'] for m in g['members']})})
    docs.sort(key=lambda x: (-x['S_sel'], x['published_at']))
    picked = [dict(x, low_confidence=False) for x in docs if x['S_sel'] >= 20][:12]
    if len(picked) < 5:
        picked += [dict(x, low_confidence=True) for x in docs if x['S_sel'] < 20][:5 - len(picked)]
    return picked


class Classifier:
    def __init__(self, model, cache_dir, client=None):
        self.model, self.client = model, client
        self.dir = Path(cache_dir) / 'ach_llm' / f'{model}_{PROMPT_VERSION}'
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tokens, self.calls = Counter(), 0

    def __call__(self, doc, company, run):
        key = hashlib.sha256(f"{doc['url']}|{squash(doc['text'])}|{company}|{self.model}|{PROMPT_VERSION}|{run}".encode()).hexdigest()
        path = self.dir / f'{key}.json'
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8'))['units']
        if self.client is None:
            lf.load_env()
            from openai import OpenAI
            self.client = OpenAI()
        kwargs = {'model': self.model, 'response_format': {'type': 'json_schema', 'json_schema': {
            'name': 'units', 'strict': True, 'schema': SCHEMA}},
            'messages': [{'role': 'system', 'content': SYSTEM},
                         {'role': 'user', 'content': f"대상 회사: {company}\n기사 입력일: {doc['published_at'][:10]}\n\n"
                                                     f"제목: {doc['title']}\n\n본문: {doc['text']}"}]}
        if self.model.startswith('gpt-5'):
            kwargs['reasoning_effort'] = 'minimal'
        resp = self.client.chat.completions.create(**kwargs)
        units = json.loads(resp.choices[0].message.content)['units']
        self.tokens.update({'prompt_tokens': resp.usage.prompt_tokens, 'completion_tokens': resp.usage.completion_tokens})
        self.calls += 1
        tmp = path.with_suffix(f'.{os.getpid()}.{threading.get_ident()}.tmp')   # 동시에 같은 기사를 써도 깨지지 않게
        tmp.write_text(json.dumps({'url': doc['url'], 'run': run, 'units': units}, ensure_ascii=False, indent=1),
                       encoding='utf-8')
        os.replace(tmp, path)
        return units


def majority(runs, text_with_title):
    """3회 실행의 단위를 인용 구절로 맞춰 유형·방향을 다수결로 정한다. 2회 이상 나온 단위만 남긴다."""
    checked = []
    for units in runs:
        ok = [u for u in units if squash(u['quote']) and squash(u['quote']) in squash(text_with_title)]
        checked.append((ok, len(units)))
    pool = [(i, u) for i, (ok, _) in enumerate(checked) for u in ok]
    used, out = set(), []
    for i, u in pool:
        if (i, id(u)) in used:
            continue
        match = {i: u}
        for j, v in pool:
            if j not in match and (j, id(v)) not in used and sim(u['quote'], v['quote']) >= 0.6:
                match[j] = v
        if len(match) < 2:
            continue
        used.update((j, id(v)) for j, v in match.items())
        types = [match[k]['type'] for k in sorted(match)]
        top, count = Counter(types).most_common(1)[0]
        ambiguous = len(match) == RUNS and count == 1
        chosen = types[0] if ambiguous else top
        base = next(match[k] for k in sorted(match) if match[k]['type'] == chosen)
        dirs = Counter(match[k]['direction'] for k in match if match[k]['type'] == chosen)
        out.append({**base, 'type': chosen, 'direction': dirs.most_common(1)[0][0], 'ambiguous': ambiguous,
                    'run_types': types, 'runs_seen': len(match)})
    quote_total = sum(n for _, n in checked)
    return out, {'units_proposed': quote_total, 'units_quote_ok': sum(len(ok) for ok, _ in checked)}


def relation(unit_type, direction, hypothesis):
    r = RELATION.get(unit_type, {}).get(hypothesis, '0')
    if direction == '긍정' and unit_type not in NO_DIRECTION:
        r = {'+': '-', '-': '+'}.get(r, r)
    return r


def merge(units):
    """설계서 5.5: (1) 같은 유형·방향 + 인용 유사도 0.9 이상, (2) 같은 방향 + 사건 이름 유사도 0.85 이상."""
    groups = []
    for u in sorted(units, key=lambda u: -u['w_doc']):
        g = next((g for g in groups if g[0]['type'] == u['type'] and g[0]['direction'] == u['direction']
                  and any(sim(u['quote'], v['quote']) >= 0.9 for v in g)), None)
        (g.append(u) if g else groups.append([u]))
    for g in [g for g in groups if g[0].get('mandatory')]:       # 같은 유형·방향의 기사 단위는 그 공시로 합친다
        for other in [o for o in groups if o is not g and not o[0].get('mandatory')
                      and o[0]['type'] == g[0]['type'] and o[0]['direction'] == g[0]['direction']]:
            g += other
            groups.remove(other)
    merged = True
    while merged:
        merged = False
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                if groups[a][0]['direction'] == groups[b][0]['direction'] and any(
                        sim(x['event'], y['event']) >= 0.85 for x in groups[a] for y in groups[b]):
                    groups[a] += groups.pop(b)
                    merged = True
                    break
            if merged:
                break
    out = []
    for i, g in enumerate(groups):
        best = max(g, key=lambda u: u['w_doc'] * u['a_u'])
        out.append({'unit_id': f'u{i + 1}', 'type': best['type'], 'direction': best['direction'], 'event': best['event'],
                    'quote': best['quote'], 'w': round(best['w_doc'] * best['a_u'] * best['g_u'], 4),
                    'r_cert': best['r_cert'], 'certainty': best['certainty'], 'unverified': best['r_cert'] <= 0.6,
                    'ambiguous': any(u['ambiguous'] for u in g), 'run_types': best['run_types'],
                    'mandatory': any(u.get('mandatory') for u in g),
                    'members': [{'bundle_id': u['bundle_id'], 'url': u['url'], 'type': u['type']} for u in g]})
    return out


# 판정 설정. 기본값은 설계서 그대로다. 바꾼 값은 튜닝(2023~2024 표본)에서만 고르고 모두 기록한다(tune.py).
PARAMS = {'gap': 0.5,                 # 5.6절 동률 차이 기준
          'positive_weight': 1.0,     # 긍정 방향 단위(좋은 소식) 가중 배수. 1이면 설계서 그대로
          'market_only_holds': False}  # True면 시장 규칙(K) 단위만으로는 판정하지 않고 판단보류


def score(units, params=None):
    params = {**PARAMS, **(params or {})}
    support, refute = defaultdict(float), defaultdict(float)
    for u in units:
        w = u['w'] * (params['positive_weight'] if u['direction'] == '긍정' and u['type'] not in NO_DIRECTION else 1.0)
        for h in HYPOTHESES:
            r = relation(u['type'], u['direction'], h)
            if r == '+':
                support[h] += w
            elif r == '-':
                refute[h] += w
    return {h: round(support[h], 4) for h in HYPOTHESES}, {h: round(refute[h], 4) for h in HYPOTHESES}


def decide(units, gap=None, params=None):
    """설계서 5.6 1~3번: 지지 > 0 후보 중 반증이 가장 적은 동률군, 그 안에서 지지 최대. 다른 묶음과 가까우면 판단보류."""
    params = {**PARAMS, **(params or {})}
    gap = params['gap'] if gap is None else gap
    support, refute = score(units, params)
    cands = [h for h in HYPOTHESES if support[h] > 0]
    base = {'support': support, 'refute': refute}
    evidence = [u for u in units if any(relation(u['type'], u['direction'], h) != '0' for h in HYPOTHESES)]
    if params['market_only_holds'] and evidence and all(u.get('market_rule') or u['type'].startswith('K') for u in evidence):
        return {**base, 'hypothesis': 'H0', 'verdict': '판단보류', 'reason': 'market_rule_only', 'runner_up': None}
    if not cands:
        return {**base, 'hypothesis': 'H0', 'verdict': '판단보류', 'reason': 'no_supported_hypothesis', 'runner_up': None}
    low = min(refute[h] for h in cands)
    tie = sorted([h for h in cands if refute[h] - low < gap], key=lambda h: -support[h])
    top = tie[0]
    rest = sorted([h for h in cands if h != top], key=lambda h: (refute[h], -support[h]))
    runner = rest[0] if rest else None
    close_other = [h for h in tie[1:] if GROUP[h] != GROUP[top] and support[top] - support[h] < gap]
    if close_other:
        return {**base, 'hypothesis': top, 'verdict': '판단보류', 'reason': 'competing_groups_too_close',
                'runner_up': close_other[0]}
    return {**base, 'hypothesis': top, 'verdict': GROUP[top], 'reason': None, 'runner_up': runner}


def judge(units, gap=None, params=None):
    main = decide(units, gap, params)
    flags = {'fragile': False, 'unverified_basis': False, 'mixed': [], 'low_confidence': False}
    top = main['hypothesis']
    if top != 'H0':
        contrib = [u for u in units if relation(u['type'], u['direction'], top) == '+']
        removable = [u for u in contrib if not u.get('mandatory')]   # 필수 공시는 빼지 않는다
        if removable:
            biggest = max(removable, key=lambda u: u['w'])
            if decide([u for u in units if u is not biggest], gap, params)['verdict'] != main['verdict']:
                flags['fragile'] = True
        if contrib:
            unverified = sum(u['w'] for u in contrib if u['unverified'])
            flags['unverified_basis'] = unverified >= 0.5 * main['support'][top] > 0
        for h in HYPOTHESES:
            if GROUP[h] != GROUP[top] and main['refute'][h] == 0 and main['support'][h] >= 0.4 * main['support'][top] > 0:
                flags['mixed'].append(h)
    for k in (1, 2):                                     # ambiguous 단위는 다른 실행의 유형으로 바꿔 다시 판정한다
        alt = [{**u, 'type': u['run_types'][k]} if u['ambiguous'] and len(u['run_types']) > k else u for u in units]
        if alt != units and decide(alt, gap, params)['verdict'] != main['verdict']:
            flags['fragile'] = True
    return main, flags


def analyze(event, docs, classify, sessions_d0_open, dart_units=(), market_units=()):
    company = event['company_name']
    raw = []
    stats = Counter()
    for d in docs:
        runs = [classify(d, company, r) for r in range(RUNS)]
        units, s = majority(runs, d['title'] + '\n' + d['text'])
        stats.update(s)
        stats['agree_type'] += sum(len(set(u['run_types'])) == 1 and u['runs_seen'] == RUNS for u in units)
        stats['units_majority'] += len(units)
        stats['seen_all_runs'] += sum(u['runs_seen'] == RUNS for u in units)          # 3회 모두 단위로 나온 것
        stats['ambiguous'] += sum(u['ambiguous'] for u in units)
        for u in units:
            if u['type'] == 'C1':
                u['direction'] = '부정'                  # 설계서: C1은 지분 희석이라 방향이 정해져 있다
            if u['type'] == 'X0':
                continue
            if u['type'] == 'M1' and float(event['index_ret_pct']) > -1.5:
                stats['m1_invalid'] += 1                # 시장이 크게 움직이지 않은 날의 거시 기사는 급락을 설명 못 한다
                continue
            r_cert, cert = certainty(u['quote'], d['title'] + '\n' + d['text'])
            late_r1 = u['type'] == 'R1' and d['session_d'] == 0 and datetime.fromisoformat(d['published_at']) >= sessions_d0_open
            raw.append({**u, 'bundle_id': d['bundle_id'], 'url': d['url'], 'r_cert': r_cert, 'certainty': cert,
                        'w_doc': d['S_sel'] / 100 * d['r_src'] * r_cert, 'a_u': 0.5 if late_r1 else 1.0, 'g_u': 1.0})
    for u in dart_units:                                 # 필수 공시: 1·2차를 거치지 않고 등급값을 가중치로 쓴다
        raw.append({**u, 'bundle_id': f"dart:{u['rcept_no']}", 'r_cert': 1.0, 'certainty': '확정', 'w_doc': u['grade'],
                    'a_u': 1.0, 'g_u': 1.0, 'ambiguous': False, 'run_types': [u['type']] * RUNS})
    units = merge(raw)
    for i, u in enumerate(market_units):                 # 시장 규칙: 병합하지 않고 0.6 고정
        units.append({**u, 'unit_id': f'k{i + 1}', 'r_cert': 1.0, 'certainty': '확정', 'unverified': False,
                      'ambiguous': False, 'run_types': [u['type']] * RUNS, 'members': []})
    main, flags = judge(units)
    flags['low_confidence'] = any(d['low_confidence'] for d in docs)
    table = defaultdict(lambda: {'n': 0, 'W': 0.0})
    for u in units:
        table[f"{u['type']}/{u['direction']}"]['n'] += 1
        table[f"{u['type']}/{u['direction']}"]['W'] = round(table[f"{u['type']}/{u['direction']}"]['W'] + u['w'], 4)
    return {**main, 'flags': flags, 'type_table': dict(table), 'units': units, 'classifier_stats': dict(stats)}


def load_inputs(cache, scores_dir):
    import p_pool
    rows = p_pool.load_rows(cache)
    by_url = {r['url']: r for rs in rows.values() for r in rs}
    with (OUT / 'drop_events.csv').open(encoding='utf-8-sig', newline='') as f:
        events = list(csv.DictReader(f))
    with (OUT / 'drop_sessions.csv').open(encoding='utf-8-sig', newline='') as f:
        sessions = defaultdict(list)
        for s in csv.DictReader(f):
            sessions[s['drop_id']].append(s)
    return by_url, events, sessions


def run(cache, scores_dir, model, drop_ids=None, workers=8, dart_by_event=None, market_by_event=None):
    by_url, events, sessions = load_inputs(cache, scores_dir)
    classify = Classifier(model, cache)
    plans = []
    for e in events:
        if drop_ids and e['drop_id'] not in drop_ids:
            continue
        path = Path(scores_dir) / f"{e['drop_id']}.json"
        score_file = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'groups': [], 'status': 'missing'}
        docs = selection(score_file['groups'], by_url, sessions[e['drop_id']]) if score_file.get('hold_reason') != 'collection_incomplete' else []
        plans.append((e, docs, score_file.get('hold_reason')))
    jobs = list({(d['url'], d['text'], e['company_name'], r): (d, e['company_name'], r)
                 for e, docs, _ in plans for d in docs for r in range(RUNS)}.values())   # 겹치는 사건의 같은 기사는 한 번만
    with ThreadPoolExecutor(workers) as pool:                # API 호출을 먼저 병렬로 채워 둔다(캐시에 저장)
        list(pool.map(lambda j: classify(*j), jobs))
    results = []
    for e, docs, hold in plans:
        base = {'drop_id': e['drop_id'], 'stock_code': e['stock_code'], 'company_name': e['company_name'], 'D': e['D'],
                'z': float(e['z']), 'rule_version': RULE_VERSION, 'prompt_version': PROMPT_VERSION, 'model': model,
                'selected_docs': [{k: v for k, v in d.items() if k != 'text'} for d in docs]}
        if hold == 'collection_incomplete':
            results.append({**base, 'hypothesis': 'H0', 'verdict': '판단보류', 'reason': 'collection_incomplete'})
            continue
        du = (dart_by_event or {}).get(e['drop_id'], {}).get('units', [])
        mu = (market_by_event or {}).get(e['drop_id'], {}).get('units', [])
        base['inputs'] = {'news_docs': len(docs), 'dart': 'missing' if dart_by_event is None else len(du),
                          'market_rules': 'missing' if market_by_event is None else len(mu)}
        if not docs and not du and not mu:
            results.append({**base, 'hypothesis': 'H0', 'verdict': '판단보류', 'reason': 'no_evidence_in_window',
                            'flags': {}, 'units': []})
            continue
        d0 = next(s for s in sessions[e['drop_id']] if s['d'] == '0')
        open_time = datetime.fromisoformat(d0['trade_date'] + 'T09:00:00+09:00')
        results.append({**base, **analyze(e, docs, classify, open_time, du, mu)})
    return results, classify


def summarize(results, classify):
    verdicts = Counter(r['verdict'] for r in results)
    reasons = Counter(r.get('reason') for r in results if r['verdict'] == '판단보류')
    with_docs = [r for r in results if r.get('units')]                  # 근거 단위가 하나라도 있는 사건
    stats = Counter()
    for r in with_docs:
        stats.update(r.get('classifier_stats', {}))
    flags = Counter(k for r in with_docs for k, v in r.get('flags', {}).items() if v)
    inputs = Counter()
    for r in results:
        i = r.get('inputs', {})
        inputs['events_with_news_docs'] += bool(i.get('news_docs'))
        inputs['events_with_dart_units'] += isinstance(i.get('dart'), int) and i['dart'] > 0
        inputs['events_with_market_units'] += isinstance(i.get('market_rules'), int) and i['market_rules'] > 0
        inputs['dart_missing'] += i.get('dart') == 'missing'
    return {'events': len(results), 'params': dict(PARAMS), 'verdicts': dict(verdicts), 'hold_reasons': dict(reasons), 'inputs': dict(inputs),
            'hypotheses': dict(Counter(r['hypothesis'] for r in results)),
            'events_with_docs': len(with_docs),
            'verdicts_with_docs': dict(Counter(r['verdict'] for r in with_docs)),
            'flags': dict(flags),
            'unit_types': dict(Counter(f"{u['type']}/{u['direction']}" for r in with_docs for u in r['units']).most_common(25)),
            'classifier': {**dict(stats),
                           'quote_pass_rate': round(stats['units_quote_ok'] / stats['units_proposed'], 3) if stats['units_proposed'] else None,
                           'type_agreement_rate': round(stats['agree_type'] / stats['units_majority'], 3) if stats['units_majority'] else None,
                           'seen_all_runs_rate': round(stats['seen_all_runs'] / stats['units_majority'], 3) if stats['units_majority'] else None,
                           'type_agreement_when_seen_all': round(stats['agree_type'] / stats['seen_all_runs'], 3) if stats['seen_all_runs'] else None,
                           'ambiguous_rate': round(stats['ambiguous'] / stats['units_majority'], 3) if stats['units_majority'] else None},
            'new_calls': classify.calls, 'new_tokens': dict(classify.tokens),
            'limits': ['V=1, N·P는 사실 기반 임시 지표, 보류 시 0.5로 채움', 'DART는 dart.py 결과가 있을 때만, 시장 규칙은 K4만(K1·K3·K2 없음), 업종 트랙 없음',
                       '병합은 문자열 유사도', '2015~2019년 19종목 표본이며 설계서의 튜닝·검증 표본(2023~2025)이 아님',
                       '정답 비교 없음: 분류 표본 정확도는 사람 검수 필요']}


def review_sample(results, n=20, seed=7):
    units = [(r['drop_id'], r['company_name'], u) for r in results for u in r.get('units', []) if u['members']]   # LLM 분류 단위만
    random.Random(seed).shuffle(units)
    return [{'drop_id': d, 'company': c, 'unit_id': u['unit_id'], 'quote': u['quote'], 'llm_type': u['type'],
             'llm_direction': u['direction'], 'event': u['event'], 'url': u['members'][0]['url'],
             'human_type': '', 'human_direction': '', 'note': ''} for d, c, u in units[:n]]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--cache', type=Path, default=HERE / 'cache/naver_full')
    ap.add_argument('--scores', type=Path, help='N·P 결과 폴더 (기본: <cache>/scores_all_hybrid/pooled)')
    ap.add_argument('--drop-id', action='append')
    ap.add_argument('--split', help='drop_events.csv의 split 열로 거른다 (예: tune). 검증용은 설정 확정 뒤에만')
    ap.add_argument('--model')
    ap.add_argument('--out', type=Path, help='결과 폴더 (기본: <cache>/ach)')
    ap.add_argument('--no-market', action='store_true', help='시장 규칙(K4) 없이 실행')
    ap.add_argument('--market-only-holds', action='store_true', help='시장 규칙 단위만 있는 사건은 판단보류 (tune.py 참고)')
    ap.add_argument('--positive-weight', type=float, help='좋은 소식 단위 가중 배수 (기본 1.0 = 설계서)')
    ap.add_argument('--gap', type=float, help='판정 동률 차이 기준 (기본 0.5 = 설계서)')
    ap.add_argument('--no-dart', action='store_true', help='DART 공시 없이 실행')
    args = ap.parse_args(argv)
    lf.load_env()
    model = args.model or os.getenv('OPENAI_ACH_MODEL') or lf.DEFAULT_MODEL
    scores = args.scores or args.cache / 'scores_all_hybrid/pooled'
    import dart
    import market_rules
    PARAMS['market_only_holds'] = args.market_only_holds or PARAMS['market_only_holds']
    if args.positive_weight is not None:
        PARAMS['positive_weight'] = args.positive_weight
    if args.gap is not None:
        PARAMS['gap'] = args.gap
    with (OUT / 'drop_events.csv').open(encoding='utf-8-sig', newline='') as f:
        events = list(csv.DictReader(f))
    ids = set(args.drop_id or [])
    if args.split:
        ids |= {e['drop_id'] for e in events if e.get('split') == args.split}
        events = [e for e in events if e['drop_id'] in ids]
    market_by_event = None if args.no_market else market_rules.event_units(events)
    dart_by_event = None if args.no_dart else dart.load_units()     # dart.py를 먼저 돌려야 생긴다
    results, classify = run(args.cache, scores, model, ids,
                            dart_by_event=dart_by_event, market_by_event=market_by_event)
    out = args.out or args.cache / 'ach'
    out.mkdir(parents=True, exist_ok=True)
    for r in results:
        (out / f"{r['drop_id']}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding='utf-8')
    summary = summarize(results, classify)
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    sample = review_sample(results)
    if sample:
        with (out / 'unit_review_sample.csv').open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(sample[0]))
            w.writeheader()
            w.writerows(sample)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
