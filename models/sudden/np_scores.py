"""사실 후보 기반 신규성 N·보도 확산 P의 임시 지표. 이전 문장 방식도 선택 가능.

DB에서 시점·관련성·유사 묶음을 다시 계산한다. 외부 API나 LLM은 사용하지 않는다.
값은 위험점수·원인 확률이 아니며 N과 P를 합산하지 않는다.
"""
import argparse
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import news_review as review
import news_facts as facts
import p_pool

HERE = Path(__file__).resolve().parent
RULE_VERSION = 'np-provisional-1'
CONFIG_PATH = HERE / 'np_config.json'


def metric(value=None, reason=None, **details):
    return {'status': 'held' if reason else 'computed', 'value': value,
            'hold_reason': reason, **details}


def eligible_rows(rows, periods, event):
    """입력의 period 표시 대신 공개·수정 시각으로 다시 판정한다."""
    return [r for r in rows if r.get('candidate_for_content_analysis')
            and review.period_of(r, event)[0] in periods
            and review.period_of(r, event)[1] == 'eligible_by_timestamp']


def novelty(group, by_url, baseline):
    current = [(by_url[m['url']], review.shingles(by_url[m['url']]['body'])) for m in group['members']]
    prior = [(a, review.shingles(a['body'])) for a in baseline]
    if not current or any(not tokens for _, tokens in current):
        return metric(reason='insufficient_event_text')
    prior = [(a, tokens) for a, tokens in prior if tokens]
    if not prior:
        return metric(reason='no_n_baseline_text', baseline_article_count=0)
    # 묶음의 모든 기사 중 과거 기사와 가장 비슷한 조합을 사용한다.
    best = max(((review.similarity(tokens, old_tokens), a['url'], old['url'])
                for a, tokens in current for old, old_tokens in prior), key=lambda x: (x[0], x[1], x[2]))
    return metric(round(1-best[0], 6), max_similarity=round(best[0], 6),
                  matched_event_url=best[1], nearest_prior_url=best[2],
                  baseline_article_count=len(prior), formula='1 - max_jaccard_5char')


def propagation(group, baseline_groups, event, max_hours):
    first = datetime.fromisoformat(group['first_published_at'])
    decision = datetime.fromisoformat(event['news_window_end'])
    base_end = datetime.fromisoformat(event['base_end'])
    horizon = min(timedelta(hours=max_hours), decision-first)
    seconds = horizon.total_seconds()
    if seconds <= 0:
        return metric(reason='no_observation_time', observation_seconds=seconds)
    members = [m for m in group['members'] if datetime.fromisoformat(m['published_at']) <= first+horizon]
    if any(not m.get('publisher') for m in members):
        return metric(reason='event_publisher_unknown', observation_seconds=seconds)
    publishers = sorted({m['publisher'] for m in members})
    references, censored, unknown = [], 0, 0
    for old in baseline_groups:
        start = datetime.fromisoformat(old['first_published_at'])
        end = start+horizon
        if end > base_end:
            censored += 1
            continue
        observed = [m for m in old['members'] if datetime.fromisoformat(m['published_at']) <= end]
        if not observed or any(not m.get('publisher') for m in observed):
            unknown += 1
            continue
        references.append({'group_id': old['group_id'], 'observation_end': end.isoformat(),
                           'publisher_count': len({m['publisher'] for m in observed})})
    details = {'publisher_count': len(publishers), 'publishers': publishers,
               'observation_seconds': seconds, 'baseline_group_count': len(references),
               'censored_baseline_groups': censored, 'unknown_publisher_baseline_groups': unknown,
               'references': references}
    if not references:
        return metric(reason='no_complete_p_baseline_groups', **details)
    counts = [g['publisher_count'] for g in references]
    less = sum(c < len(publishers) for c in counts)
    equal = sum(c == len(publishers) for c in counts)
    return metric(round((less+0.5*equal)/len(counts), 6), less_count=less, tie_count=equal,
                  formula='(less + 0.5 * ties) / baseline_group_count', **details)


def score(report, rows, config, pool=None):
    method = config.get('news_representation', 'lexical-v1')
    if method not in ('lexical-v1', 'facts-v1'):
        raise ValueError('news_representation은 lexical-v1 또는 facts-v1이어야 합니다')
    fact_mode = method == 'facts-v1'
    min_fact_p = config.get('facts_min_p_baseline_groups', 20)
    if isinstance(min_fact_p, bool) or not isinstance(min_fact_p, int) or min_fact_p < 1:
        raise ValueError('facts_min_p_baseline_groups는 양의 정수여야 합니다')
    similarity_cut = config.get('group_similarity_cut', review.SIMILARITY_CUT)
    if isinstance(similarity_cut, bool) or not isinstance(similarity_cut, (int, float)) or not 0 < similarity_cut <= 1:
        raise ValueError('group_similarity_cut는 0 초과 1 이하여야 합니다')
    max_hours = config['p_observation_max_hours']
    if isinstance(max_hours, bool) or not isinstance(max_hours, (int, float)) or not 0 < max_hours <= review.GROUP_HOURS:
        raise ValueError('p_observation_max_hours는 0 초과, 유사 묶음 최대 시간(72시간) 이하여야 합니다')
    warning_min = config['small_baseline_warning_below']
    if isinstance(warning_min, bool) or not isinstance(warning_min, int) or warning_min < 1:
        raise ValueError('small_baseline_warning_below는 양의 정수여야 합니다')
    scope = config.get('p_baseline_scope', 'own')
    if scope not in ('own', 'pooled'):
        raise ValueError('p_baseline_scope는 own 또는 pooled여야 합니다')
    if scope == 'pooled' and pool is None:
        raise ValueError('p_baseline_scope=pooled에는 p_pool.build_pool 결과가 필요합니다')
    pool_days, p_scale = config.get('p_pool_days', 365), config.get('p_scale', 'none')
    event = report['event']
    result = {'drop_id': report['drop_id'], 'rule_version': 'np-facts-provisional-2' if fact_mode else RULE_VERSION,
              'news_representation': method,
              'review_rule_version': review.RULE_VERSION, 'scale_status': 'provisional', 'config': config,
              'review_settings': {'similarity_cut': similarity_cut, 'group_hours': review.GROUP_HOURS,
                                  'character_shingle_length': 5},
              'coverage': report['coverage'], 'event': event, 'groups': [], 'warnings': [],
              'integrated': {'status': 'not_computed', 'reason': 'N/P는 합산하거나 위험점수로 환산하지 않음'},
              'limits': ['N은 제한된 규칙으로 추출한 행동·대상·세부 정보의 새로움 후보이며 사실 검증 결과가 아님' if fact_mode else 'N은 문장 차이이며 주제나 사실의 최초 공개 여부를 판정하지 않음',
                         'P는 네이버 수록 기업 소식의 언론사 확산이며 경제적 영향력·독립 취재 수가 아님',
                         '외부 전용 기사와 자동 시세 본문은 제외됨',
                         '분류·유사 묶음·관찰시간 설정은 초기 규칙이며 수정 시각 부재가 과거 본문을 보장하지 않음']}
    if not report['ready_for_baseline_analysis']:
        result.update(status='held', hold_reason='collection_incomplete')
        return result
    current = eligible_rows(rows, {'event_window'}, event)
    n_base = eligible_rows(rows, {'p_base', 'n_base_extra'}, event)
    # 오래된 검토 JSON에 저장된 groups도 쓰지 않고 검증된 기사로 묶음을 다시 만든다.
    cluster = facts.cluster if fact_mode else review.cluster
    groups = cluster(current, cutoff=similarity_cut)
    # 90일 시작 전에 이미 보도된 묶음의 꼬리를 신규 기준 묶음으로 세지 않는다.
    p_start = datetime.fromisoformat(event['p_base_start'])
    p_groups = [g for g in cluster(n_base, cutoff=similarity_cut) if datetime.fromisoformat(g['first_published_at']) >= p_start
                and (not fact_mode or g.get('grouping_method') == facts.VERSION)]
    by_url = {a['url']: a for a in current}
    digest_input = [{k:a.get(k) for k in ('url', 'title', 'body', 'published_at', 'modified_at', 'publisher')}
                    for a in sorted(current+n_base, key=lambda a:a['url'])]
    result['input_sha256'] = hashlib.sha256(json.dumps(digest_input, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if not groups:
        result.update(status='held', hold_reason='no_company_news_in_window')
        return result
    for group in groups:
        n = (facts.novelty if fact_mode else novelty)(group, by_url, n_base)
        if fact_mode and group.get('grouping_method') != facts.VERSION:
            p = metric(reason='fact_extraction_needs_review')
        elif scope == 'pooled':
            p = p_pool.pooled_propagation(group, event, pool, max_hours, pool_days, p_scale,
                                          min_fact_p if fact_mode else 1)
        else:
            p = propagation(group, p_groups, event, max_hours)
        if scope == 'own' and fact_mode and p['status'] == 'computed' and p['baseline_group_count'] < min_fact_p:
            p.update(status='held', hold_reason='fact_reference_sample_too_small',
                     diagnostic_percentile=p['value'], value=None, minimum_reference_groups=min_fact_p)
        result['groups'].append({**group, 'N': n, 'P': p})
        n_count = n.get('supported_baseline_articles', n.get('baseline_article_count'))
        p_count = p.get('baseline_group_count')
        if (n_count is not None and n_count < warning_min) or (p_count is not None and p_count < warning_min):
            result['warnings'].append({'group_id': group['group_id'], 'reason': 'small_baseline'})
    if fact_mode:
        result['fact_rule_version'] = facts.VERSION
        result['fact_extraction'] = {'event_articles': len(current),
            'supported_event_articles': sum(bool(facts.fact_list(facts.extract(a))) for a in current),
            'baseline_articles': len(n_base),
            'supported_baseline_articles': sum(bool(facts.fact_list(facts.extract(a))) for a in n_base)}
    statuses = [g[k]['status'] for g in result['groups'] for k in ('N', 'P')]
    result['status'] = 'computed' if all(s == 'computed' for s in statuses) else ('held' if all(s == 'held' for s in statuses) else 'partial')
    result['hold_reason'] = None if result['status'] == 'computed' else 'one_or_more_metrics_held'
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--drop-id', default='053660_20181219')
    ap.add_argument('--cache', type=Path, default=HERE / 'cache/naver')
    ap.add_argument('--config', type=Path, default=CONFIG_PATH)
    args = ap.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding='utf-8'))
        report, rows = review.build_report(args.drop_id, args.cache)
        pool = p_pool.build_pool(p_pool.load_rows(args.cache)) if config.get('p_baseline_scope') == 'pooled' else None
        result = score(report, rows, config, pool)
    except (ValueError, KeyError) as exc:
        ap.error(str(exc))
    out = args.cache / 'scores'
    out.mkdir(exist_ok=True)
    path = out / f'{args.drop_id}.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps({'drop_id': result['drop_id'], 'status': result['status'],
                      'groups': [{'title': g['title'], 'N': g['N'], 'P': {k:v for k,v in g['P'].items() if k != 'references'}}
                                 for g in result['groups']], 'warnings': result['warnings'], 'saved_to': str(path)}, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'computed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
