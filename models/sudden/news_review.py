"""수집 뉴스 관련성·가격 반응 분류와 시점별 유사 기사 묶음. LLM/외부 접속 없음."""
import os
import argparse
import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / os.getenv('SUDDEN_OUTPUT', 'output')     # 다른 표본(예: output_2023)을 쓸 때 바꾼다
RULE_VERSION = 'news-review-1'
# 동일 사건이라는 확정 판정이 아닌, 사람이 확인 가능한 유사 보도 후보 묶음이다.
SIMILARITY_CUT = 0.42
GROUP_HOURS = 72
MATERIAL = re.compile(r'공시|계약|수주|취득|인수|매각|발행|증자|감자|개소|채굴|협약|실적|적자|흑자|회생|파산|횡령|배임|소송|감사|상장폐지|거래정지')
PRICE = re.compile(r'급락|급등|하한가|상한가|신저가|신고가|거래량|현재가|하락|상승|강세|약세|거래중|거래 중')
ROUNDUP = re.compile(r'코스피|코스닥|마감|주요\s*종목|기업공시|증시|시황')
AUTOMATED = re.compile(r'라이온봇|서경뉴스봇|fnRASSI|인공지능.*실시간|로봇기자', re.I)
FOOTER = re.compile(r'▶|저작권자|무단\s*전재|네이버에서|네이버 홈에서|\b[\w.+-]+@[\w.-]+\.[a-z]{2,}', re.I)


def automated_title(title):
    """검증된 자동 시세 제목 형식만 본문 수집 제외 후보로 삼는다."""
    if MATERIAL.search(title):
        return False
    return bool(re.search(r'^<코>|^\[fnRASSI\]|^\[정오 시황\]|^오[전후] \d{1,2}:\d{2} 현재 코스닥', title))


def clean_body(body):
    text = FOOTER.split(body)[0]
    text = re.sub(r'^\s*\[[^\]]{1,100}\]\s*', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def classify(article, stock_code, names):
    title, body = article.get('title', ''), article.get('body', '')
    title_names = [name for name in names if name and name in title]
    body_names = [name for name in names if name and name in body]
    code = bool(re.search(r'(?<!\d)' + re.escape(stock_code) + r'(?!\d)', title + ' ' + body))
    if title_names:
        relevance = 'company_focus'
    elif body_names or code:
        relevance = 'mentioned'
    else:
        relevance = 'unrelated'
    automated = bool(AUTOMATED.search(title + ' ' + body))
    material = bool(MATERIAL.search(title))
    if relevance == 'unrelated':
        kind = 'unrelated'
    elif not title_names and ROUNDUP.search(title):
        kind = 'roundup'
    elif PRICE.search(title) and not material:
        kind = 'price_reaction'
    elif automated and not material:
        kind = 'price_reaction'
    elif relevance == 'company_focus':
        kind = 'company_news'
    else:
        kind = 'mention_review'
    return {'relevance': relevance, 'kind': kind, 'automated': automated,
            'matched_names': sorted(set(title_names + body_names)), 'stock_code_present': code,
            'candidate_for_content_analysis': kind == 'company_news',
            'reason': {'company_news': '제목에서 회사 이름 확인', 'price_reaction': '가격 움직임 또는 자동 시세 기사',
                       'roundup': '여러 종목을 다루는 시황·공시 모음', 'mention_review': '본문 언급만 있어 관련성 확인 필요',
                       'unrelated': '회사 이름과 종목코드 확인 안 됨'}[kind]}


def shingles(text, n=5):
    compact = re.sub(r'[^가-힣a-z0-9]', '', clean_body(text).lower())
    return {compact[i:i+n] for i in range(max(0, len(compact)-n+1))}


def similarity(left, right):
    return len(left & right) / len(left | right) if left and right else 0.0


def quantities(text):
    values = {}
    for number, scale, unit in re.findall(r'(\d+(?:\.\d+)?)\s*(억|만)?\s*(원|주|%)', clean_body(text).replace(',', '')):
        value = float(number) * {'억': 100000000, '만': 10000, '': 1}[scale]
        values.setdefault(unit, set()).add(value)
    return values


def conflicting_quantities(left, right):
    return any(not left[unit] & right[unit] for unit in left.keys() & right.keys())


def cluster(rows, cutoff=SIMILARITY_CUT):
    """시간순 최초 기사에 직접 유사한 기사만 붙인다. 연쇄 결합은 하지 않는다."""
    groups = []
    for row in sorted(rows, key=lambda r: (r['published_at'], r['url'])):
        tokens = shingles(row['body'])
        numeric = quantities(row['body'])
        published = datetime.fromisoformat(row['published_at'])
        match, score = None, 0.0
        for group in groups:
            delta = published - datetime.fromisoformat(group['first_published_at'])
            if delta > timedelta(hours=GROUP_HOURS):
                continue
            if conflicting_quantities(numeric, group['_quantities']):
                continue
            current = similarity(tokens, group['_tokens'])
            if current >= cutoff and current > score:
                match, score = group, current
        if match is None:
            match = {'group_id': hashlib.sha256(row['url'].encode()).hexdigest()[:16],
                     'representative_url': row['url'], 'first_published_at': row['published_at'],
                     'title': row['title'], 'members': [], '_tokens': tokens, '_quantities': numeric}
            groups.append(match)
            score = 1.0
        match['members'].append({'url': row['url'], 'publisher': row.get('publisher'),
                                 'published_at': row['published_at'], 'similarity_to_first': round(score, 4)})
    for group in groups:
        group.pop('_tokens')
        group.pop('_quantities')
        group['article_count'] = len(group['members'])
        group['publishers'] = sorted({m['publisher'] for m in group['members'] if m['publisher']})
        group['publisher_count'] = len(group['publishers'])
        group['status'] = 'similar_coverage_candidate' if group['article_count'] > 1 else 'single_article'
    return groups


def period_of(article, event):
    if not article.get('published_at'):
        return None, 'time_unknown'
    t = datetime.fromisoformat(article['published_at'])
    bounds = {k: datetime.fromisoformat(event[k]) for k in
              ('n_base_start', 'p_base_start', 'base_end', 'news_window_end')}
    if bounds['base_end'] < t <= bounds['news_window_end']:
        period, end = 'event_window', bounds['news_window_end']
    elif bounds['n_base_start'] <= t <= bounds['base_end']:
        period = 'p_base' if t >= bounds['p_base_start'] else 'n_base_extra'
        end = bounds['base_end']
    else:
        return None, 'outside_period'
    modified = article.get('modified_at')
    if modified and datetime.fromisoformat(modified) > end:
        return period, 'modified_after_cutoff'
    return period, 'eligible_by_timestamp'


def prepare(articles, event, names):
    reviewed = []
    for article in articles:
        if article.get('status') != 'complete':
            continue
        period, temporal = period_of(article, event)
        reviewed.append({**article, **classify(article, event['stock_code'], names),
                         'period': period, 'temporal_status': temporal, 'rule_version': RULE_VERSION})
    scopes = {'event_window': {'event_window'}, 'p_base': {'p_base'}, 'n_base': {'p_base', 'n_base_extra'}}
    datasets = {}
    for scope, periods in scopes.items():
        rows = [r for r in reviewed if r['period'] in periods and r['temporal_status'] == 'eligible_by_timestamp']
        company = [r for r in rows if r['candidate_for_content_analysis']]
        datasets[scope] = {'article_count': len(rows), 'kind_counts': dict(Counter(r['kind'] for r in rows)),
                           'company_news_count': len(company), 'groups': cluster(company)}
        datasets[scope]['group_count'] = len(datasets[scope]['groups'])
    return reviewed, datasets


def coverage(searches, event):
    """동일 날짜의 다른 사명 검색까지 예정 작업이 모두 완료됐는지 확인한다."""
    from types import SimpleNamespace
    import naver_crawl as crawl
    args = SimpleNamespace(stock=event['stock_code'], start=event['n_base_start'][:10],
                           end=event['news_window_end'][:10], max_priority=3)
    expected = {(r['date'], q) for r, q in crawl.tasks(OUT / 'crawl_plan.csv', args)}
    by_key = {(s['date'], s['query']): s for s in searches if s['stock_code'] == event['stock_code']}
    missing = sorted(expected - by_key.keys())
    unfinished = [{'date': d, 'query': q, 'status': by_key[d, q]['status']}
                  for d, q in sorted(expected & by_key.keys()) if by_key[d, q]['status'] not in ('complete', 'empty', 'capped')]   # capped: 의도한 페이지 상한
    return {'expected_searches': len(expected), 'completed_searches': len(expected)-len(missing)-len(unfinished),
            'missing_searches': [{'date': d, 'query': q} for d, q in missing], 'unfinished_searches': unfinished,
            'search_coverage_complete': not missing and not unfinished,
            'capped_searches': sum(by_key[k]['status'] == 'capped' for k in expected & by_key.keys()),
            'external_only_results': sum(not r['naver_url'] for k,s in by_key.items() if k in expected for r in s.get('results', []))}


def build_report(drop_id, cache):
    """현재 DB에서 재계산한다. 점수 계산에서도 오래된 검토 JSON을 신뢰하지 않는다."""
    cache = Path(cache)
    with (OUT / 'drop_events.csv').open(encoding='utf-8-sig', newline='') as f:
        event = next((r for r in csv.DictReader(f) if r['drop_id'] == drop_id), None)
    if event is None:
        raise ValueError('사건 ID를 찾지 못했습니다')
    with (OUT / 'stock_names.csv').open(encoding='utf-8-sig', newline='') as f:
        names = [r['company_name'] for r in csv.DictReader(f) if r['stock_code'] == event['stock_code']]
    db_path = cache / 'crawl.sqlite3'
    if not db_path.exists():
        raise ValueError('수집 DB가 없습니다')
    with sqlite3.connect(db_path) as db:
        searches = [json.loads(p) for p, in db.execute('SELECT payload FROM searches')]
        scope_searches = [s for s in searches if s['stock_code'] == event['stock_code']
                         and event['n_base_start'][:10] <= s['date'] <= event['news_window_end'][:10]]
        urls = {r['naver_url'] for s in scope_searches for r in s.get('results', []) if r['naver_url']}
        articles = [json.loads(p) for p, in db.execute('SELECT payload FROM articles')]
        articles = [a for a in articles if a['url'] in urls]
    reviewed, datasets = prepare(articles, event, names)
    cover = coverage(searches, event)
    pending = urls - {a['url'] for a in articles if a.get('status') in ('complete', 'excluded_automated', 'excluded_title_mismatch')}
    cover['unavailable_article_count'] = len(pending)
    cover['title_excluded_article_count'] = sum(a.get('status') == 'excluded_automated' for a in articles)
    report = {'drop_id': drop_id, 'rule_version': RULE_VERSION, 'event': event,
              'coverage': cover, 'datasets': datasets,
              'article_count_basis': 'article_count는 시각 검증 가능한 수집 본문 수이며 제목으로 생략한 자동 시세 본문은 제외. 내용 분석은 company_news 후보만 사용.',
              'ready_for_baseline_analysis': cover['search_coverage_complete'] and not pending,
              'limits': ['유사 보도 후보 묶음은 전재 확정이나 인과관계 판정이 아님',
                         '외부 전용 기사 본문은 미수집', '입력 시각·수정 시각은 당시 본문 복원의 보장이 아님',
                         '관련성·분류·유사도 기준은 초기 규칙이며 N/P 점수와 급락 원인은 아직 계산하지 않음']}
    return report, reviewed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--drop-id', default='053660_20181219')
    ap.add_argument('--cache', type=Path, default=HERE / 'cache/naver')
    args = ap.parse_args(argv)
    try:
        report, reviewed = build_report(args.drop_id, args.cache)
    except ValueError as exc:
        ap.error(str(exc))
    cover, datasets = report['coverage'], report['datasets']
    out = args.cache / 'review'
    out.mkdir(exist_ok=True)
    (out / f'{args.drop_id}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    with (out / f'{args.drop_id}_articles.jsonl').open('w', encoding='utf-8') as f:
        for r in reviewed:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    compact_cover = {k:v for k,v in cover.items() if k not in ('missing_searches', 'unfinished_searches')}
    compact_cover.update(missing_search_count=len(cover['missing_searches']), unfinished_search_count=len(cover['unfinished_searches']))
    print(json.dumps({**compact_cover, 'ready_for_baseline_analysis': report['ready_for_baseline_analysis'],
                      'datasets': {k: {a:b for a,b in v.items() if a != 'groups'} for k,v in datasets.items()}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
