"""LLM(OpenAI)으로 기사 사실 후보를 뽑아 규칙 추출(news_facts.py)과 비교한다.

규칙과 같은 모양({'primary', 'facts', 'reason'})으로 돌려준다. 그래서 같은 평가기로 채점할 수 있다.
규칙이 모르는 종류의 소식은 action='other'로 받아 종류만 센다(P/N 계산에는 쓰지 않는다).

[미래 정보 차단] 지시문에서 기사 본문 밖의 지식을 쓰지 말라고 하고, 코드로도 확인한다.
    모델이 낸 근거 문구(evidence)가 제목·본문에 그대로 없으면 그 사실은 버린다.
[재현] 같은 기사·모델·지시문 버전의 답은 cache/.../llm_facts/에 저장해 다시 묻지 않는다.

[실행]
    python models/sudden/llm_facts.py --holdout      # v3 검토 기사 21건: 규칙 vs LLM 정확도
    python models/sudden/llm_facts.py --coverage     # 사건 직전 기업 소식: 사실을 뽑은 기사 수
모델은 --model, 없으면 .env의 OPENAI_FACTS_MODEL, 그것도 없으면 DEFAULT_MODEL.
"""
import argparse
import csv
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

import evaluate_holdout as holdout
import news_facts as facts
import news_review as review

HERE = Path(__file__).resolve().parent
OUT = HERE / os.getenv('SUDDEN_OUTPUT', 'output')     # 다른 표본(예: output_2023)을 쓸 때 바꾼다
ROOT = HERE.parents[1]
DEFAULT_MODEL = 'gpt-5-nano'                                # 2026-11 기준 이 계정에서 쓸 수 있는 가장 싼 계열
PROMPT_VERSION = 'llm-facts-2'
ACTIONS = ['supply_contract', 'equity_issue', 'equity_acquire', 'cb_issue', 'mou', 'office_open', 'other']
STAGES = ['announced', 'planned', 'completed', 'delayed', 'cancelled']
FIELDS = ['amount_krw', 'currency', 'counterparty', 'contract_start', 'contract_end', 'delivery_region',
          'shares', 'stake_pct']

SYSTEM = """너는 한국 경제 기사에서 회사가 한 행동(사실 후보)을 정리하는 도구다.
규칙:
- 주어진 제목과 본문에 적힌 내용만 쓴다. 기사 밖의 지식, 나중에 일어난 일, 추측은 절대 쓰지 않는다.
- 제목의 핵심 행동만 뽑는다. 본문 배경에 나오는 다른 거래는 뽑지 않는다.
- 업황 전망, 주가 움직임, 시황, 부인·미확정 소식은 사실이 아니다(has_fact=false).
- 계약이 여러 건이면 facts에 건마다 따로 적는다. 아니면 1건만 적는다.
- action: supply_contract(수주·공급계약), equity_issue(유상증자), equity_acquire(주식·지분 취득),
  cb_issue(전환사채 발행), mou(업무협약), office_open(사무소 개소), other(그 밖의 회사 행동).
- other면 other_kind에 짧은 한국어 이름(예: 무상증자, 자사주 취득, 신제품 출시, 실적 발표)을 쓴다.
- actor: 행동한 회사 이름을 기사 표기 그대로.
- target: supply_contract는 공급물(제품·설비 이름, 기사 표기 그대로), equity_acquire는 취득 대상 회사,
  mou는 협약 상대, office_open은 사무소 이름, equity_issue는 paid_in_equity,
  cb_issue는 convertible_bond(영구채면 perpetual_cb).
- stage: announced(발표·체결), planned(예정·검토), completed(완료), delayed(연기), cancelled(철회·취소).
- amount_krw: 원 단위 숫자만(예: 61억1742만원 → 6117420000). 외화면 원화 환산액이 기사에 있을 때만.
- currency: 계약 통화(KRW, USD 등). contract_start/contract_end: YYYY-MM-DD. shares: 주식 수 숫자.
  stake_pct: 지분율 숫자. counterparty: 계약·발행 상대방 이름. delivery_region: 공급 지역.
- 날짜의 연도가 기사에 없으면 기사 입력일 기준으로 정한다. 그래도 알 수 없으면 null.
- 기사에 없는 값은 null.
- evidence: 그 사실을 보여주는 문구를 제목이나 본문에서 한 글자도 바꾸지 말고 20~80자 복사한다."""

NULLABLE = {'type': ['string', 'null']}
SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['has_fact', 'facts'],
    'properties': {
        'has_fact': {'type': 'boolean'},
        'facts': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['actor', 'action', 'other_kind', 'target', 'stage', 'fields', 'evidence'],
            'properties': {
                'actor': {'type': 'string'}, 'action': {'type': 'string', 'enum': ACTIONS},
                'other_kind': NULLABLE, 'target': NULLABLE, 'stage': {'type': 'string', 'enum': STAGES},
                'fields': {'type': 'object', 'additionalProperties': False, 'required': FIELDS,
                           'properties': {k: NULLABLE for k in FIELDS}},
                'evidence': {'type': 'string'}}}}}}


def load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
    except ImportError:
        pass


def ask(article, model, client):
    messages = [{'role': 'system', 'content': SYSTEM},
                {'role': 'user', 'content': f"제목: {article.get('title', '')}\n\n본문: {review.clean_body(article.get('body', ''))}"}]
    kwargs = {'model': model, 'messages': messages,
              'response_format': {'type': 'json_schema', 'json_schema': {'name': 'facts', 'strict': True, 'schema': SCHEMA}}}
    if model.startswith('gpt-5'):
        kwargs['reasoning_effort'] = 'minimal'               # 추출만 하므로 추론을 최소로 해 비용을 줄인다
    resp = client.chat.completions.create(**kwargs)
    msg = resp.choices[0].message
    if getattr(msg, 'refusal', None):
        raise RuntimeError('모델이 답을 거절했습니다: ' + msg.refusal)
    usage = resp.usage
    return json.loads(msg.content), {'prompt_tokens': usage.prompt_tokens, 'completion_tokens': usage.completion_tokens}


def plausible_year(value, text, article):
    """기사에 없는 연도를 지어내는 것을 막는다. 연도는 기사에 있거나 입력 연도·다음 해여야 한다."""
    m = re.match(r'(\d{4})-\d{2}-\d{2}$', value)
    if not m:
        return False
    published = int((article.get('published_at') or '0000')[:4])
    return m[1] in text or int(m[1]) in (published, published + 1)


def to_rule_shape(answer, article):
    """규칙 추출과 같은 모양으로 바꾸고, 근거 문구가 기사에 없는 사실은 버린다."""
    text = article.get('title', '') + '\n' + article.get('body', '')
    squashed = ''.join(text.split())
    kept, other, dropped, dropped_fields, dropped_no_target = [], [], 0, [], 0
    for f in answer.get('facts', []) if answer.get('has_fact') else []:
        if ''.join(f['evidence'].split()) not in squashed:
            dropped += 1
            continue
        if f['action'] == 'other':
            other.append(f['other_kind'] or '기타')
            continue
        fields = {k: v.strip() for k, v in f['fields'].items() if v and v.strip().lower() not in ('', 'null', 'none')}
        for k in ('contract_start', 'contract_end'):
            if k in fields and not plausible_year(fields[k], text, article):
                fields.pop(k)
                dropped_fields.append(k)
        target = (f['target'] or '').strip()
        if target.lower() in ('', 'null', 'none'):              # 규칙도 대상을 못 찾으면 보류한다
            dropped_no_target += 1
            continue
        if f['action'] in ('supply_contract', 'office_open'):
            target = re.sub(r'\s+', '', target)             # 규칙도 공급물 이름의 띄어쓰기를 없앤다
        kept.append({'actor': re.sub(r'\s*\(\d{6}\)$', '', f['actor'].strip()), 'action': f['action'],
                     'target': target, 'stage': f['stage'],
                     'fields': fields, 'evidence': {'all': f['evidence']}, 'amount_resolution_krw': '1',
                     'extracted_by': 'llm'})
    out = {'version': PROMPT_VERSION, 'status': 'needs_review', 'primary': None, 'facts': [],
           'reason': None, 'context': [], 'other_kinds': other, 'dropped_unquoted_facts': dropped,
           'dropped_implausible_fields': dropped_fields, 'dropped_no_target': dropped_no_target}
    if kept:
        out.update(status='extracted_candidate', primary=kept[0], facts=kept if len(kept) > 1 else [])
    else:
        out['reason'] = ('llm_other_action' if other else 'llm_evidence_not_in_article' if dropped
                         else 'llm_target_not_identified' if dropped_no_target else 'llm_no_fact')
    return out


class Extractor:
    def __init__(self, model, cache_dir, client=None):
        self.model, self.dir = model, Path(cache_dir) / 'llm_facts' / f'{model}_{PROMPT_VERSION}'
        self.dir.mkdir(parents=True, exist_ok=True)
        self.client, self.tokens, self.calls = client, Counter(), 0

    def __call__(self, article):
        key = hashlib.sha256((holdout.article_hash(article) + self.model + PROMPT_VERSION).encode()).hexdigest()
        path = self.dir / f'{key}.json'
        if path.exists():
            saved = json.loads(path.read_text(encoding='utf-8'))
        else:
            if self.client is None:
                load_env()
                if not os.getenv('OPENAI_API_KEY'):
                    raise RuntimeError('OPENAI_API_KEY가 없습니다. 저장소 맨 위 .env 에 넣어 주세요.')
                from openai import OpenAI
                self.client = OpenAI()
            answer, tokens = ask(article, self.model, self.client)
            self.tokens.update(tokens)
            self.calls += 1
            saved = {'url': article['url'], 'model': self.model, 'prompt_version': PROMPT_VERSION,
                     'answer': answer, 'tokens': tokens}
            path.write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding='utf-8')
        return to_rule_shape(saved['answer'], article)


class Hybrid:
    """규칙을 먼저 쓰고, 규칙이 행동 종류를 못 알아본 기사(unsupported_title_action)에만 LLM 답을 쓴다.

    규칙이 행동은 알아봤지만 금액·상대방이 불확실해 보류한 기사, 부인·미확정 기사는 LLM으로 채우지 않는다.
    rule은 원래 규칙 함수(facts.extract를 바꿔 끼우기 전에 받아 둔다).
    """
    def __init__(self, extractor, rule=None):
        self.llm, self.rule = extractor, rule or facts.extract
        self.memo, self.used = {}, Counter()

    def __call__(self, article):
        key = holdout.article_hash(article)
        if key not in self.memo:
            out = self.rule(article)
            if not facts.fact_list(out) and out.get('reason') == 'unsupported_title_action':
                out = {**self.llm(article), 'rule_reason': out['reason']}
                self.used['llm_fact' if facts.fact_list(out) else 'llm_no_fact'] += 1
            else:
                self.used['rule_fact' if facts.fact_list(out) else 'rule_hold'] += 1
            self.memo[key] = out
        return self.memo[key]


def run_holdout(extractor, cache, plan, annotations):
    """같은 평가기로 규칙과 LLM을 채점한다. 이미 규칙 보완에 쓴 기사라 규칙 쪽은 회귀 확인이다."""
    plan, annotation = json.loads(Path(plan).read_text(encoding='utf-8')), json.loads(Path(annotations).read_text(encoding='utf-8'))
    rules = holdout.run(Path(cache), plan, annotation, allow_rule_changes=True)
    original = facts.extract
    facts.extract = extractor                                 # cluster()도 이 함수를 쓰므로 묶음까지 LLM 기준이 된다
    try:
        llm = holdout.run(Path(cache), plan, annotation, allow_rule_changes=True)
    finally:
        facts.extract = original

    def brief(result):
        rows = [r for c in result['cases'] if c['status'] == 'evaluated' for r in c['evaluation']['articles']]
        no_target = sum(r['has_primary_fact'] and r['extracted'] and all(e.startswith('target:') for e in r['errors'])
                        for r in rows)
        return {**result['summary'], 'correct_ignoring_target_name': no_target,
                'spurious_facts': sum('spurious_fact' in r['errors'] for r in rows),
                'errors': [{'url': r['url'], 'errors': r['errors']} for r in rows if r['errors']]}
    return {'rules': brief(rules), 'llm': brief(llm),
            'limits': ['라벨은 규칙 개발 중 AI 보조로 만든 것이며 사람 검수 전임',
                       '규칙은 이 기사들로 보완된 적이 있어 규칙 쪽 성적은 실제보다 좋게 나올 수 있음',
                       'target 이름은 규칙의 표기 관례(예: nuclear_waste_container)를 따라서 LLM에 불리함']}


def run_coverage(extractor, cache):
    """사건 직전(사건 창) 기업 소식에서 규칙과 LLM이 각각 사실을 뽑은 기사 수."""
    import p_pool
    rows = p_pool.load_rows(cache)
    with (OUT / 'drop_events.csv').open(encoding='utf-8-sig', newline='') as f:
        events = [e for e in csv.DictReader(f) if e.get('base_end')]
    window, event_of = {}, {}
    for e in events:
        for r in rows.get(e['stock_code'], []):
            if review.period_of(r, e) == ('event_window', 'eligible_by_timestamp'):
                window[r['url']] = r
                event_of.setdefault(r['url'], set()).add(e['drop_id'])
    rule_hit, llm_hit, other_hit, both, kinds, actions = set(), set(), set(), 0, Counter(), Counter()
    for url, a in sorted(window.items()):
        r, l = facts.fact_list(facts.extract(a)), extractor(a)
        lf = facts.fact_list(l)
        rule_hit |= {url} if r else set()
        llm_hit |= {url} if lf else set()
        other_hit |= {url} if l['other_kinds'] else set()
        both += bool(r) and bool(lf)
        kinds.update(l['other_kinds'])
        actions.update(f['action'] for f in lf)
    events_with = lambda hits: len({d for u in hits for d in event_of[u]})
    return {'window_company_news_articles': len(window), 'events': len(events),
            'rules': {'articles_with_fact': len(rule_hit), 'events_with_fact': events_with(rule_hit)},
            'llm': {'articles_with_fact': len(llm_hit), 'events_with_fact': events_with(llm_hit),
                    'by_action': dict(actions.most_common())},
            'both_articles': both,
            'llm_other_kind': {'articles': len(other_hit), 'events': events_with(other_hit),
                               'top_kinds': dict(kinds.most_common(15))},
            'llm_or_other_events': events_with(llm_hit | other_hit),
            'limits': ['LLM 사실은 정답과 비교하지 않은 개수이며 정확도가 아님', 'other 종류는 P/N 계산에 아직 쓰지 않음']}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--holdout', action='store_true')
    ap.add_argument('--coverage', action='store_true')
    ap.add_argument('--model')
    ap.add_argument('--holdout-cache', type=Path, default=HERE / 'cache/naver')
    ap.add_argument('--cache', type=Path, default=HERE / 'cache/naver_full')
    ap.add_argument('--plan', type=Path, default=HERE / 'eval/plan_v3.json')
    ap.add_argument('--annotations', type=Path, default=HERE / 'eval/annotations_v3.json')
    args = ap.parse_args(argv)
    if not args.holdout and not args.coverage:
        ap.error('--holdout 또는 --coverage를 지정하세요')
    load_env()
    model = args.model or os.getenv('OPENAI_FACTS_MODEL') or DEFAULT_MODEL
    out = args.cache / 'validation'
    out.mkdir(parents=True, exist_ok=True)
    if args.holdout:
        ex = Extractor(model, args.holdout_cache)
        result = {'model': model, 'prompt_version': PROMPT_VERSION,
                  **run_holdout(ex, args.holdout_cache, args.plan, args.annotations)}
        result['new_calls'], result['new_tokens'] = ex.calls, dict(ex.tokens)
        (out / 'llm_facts_holdout.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in result.items()}, ensure_ascii=False, indent=2))
    if args.coverage:
        ex = Extractor(model, args.cache)
        result = {'model': model, 'prompt_version': PROMPT_VERSION, **run_coverage(ex, args.cache)}
        result['new_calls'], result['new_tokens'] = ex.calls, dict(ex.tokens)
        (out / 'llm_facts_coverage.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
