"""여러 급락 사건의 N/P 재계산과 설정 민감도 확인. 수익률 예측 성능 검증은 아니다."""
import argparse
import json
from pathlib import Path

import news_review as review
import np_scores as np

HERE = Path(__file__).resolve().parent
DEFAULT_CASES = ['053660_20181105', '053660_20181121', '053660_20181219']


def compact(result):
    return {'status': result['status'], 'hold_reason': result['hold_reason'],
            'warnings': result['warnings'], 'group_count': len(result['groups']),
            'groups': [{'group_id': g['group_id'], 'title': g['title'],
                        'member_urls': sorted(m['url'] for m in g['members']),
                        'member_keys': sorted(json.dumps([m['url'],m.get('fact_index',0)]) for m in g['members']),
                        'article_count': g['article_count'], 'publisher_count': g['publisher_count'],
                        'N': g['N']['value'], 'N_status': g['N']['status'],
                        'N_hold_reason': g['N']['hold_reason'],
                        'N_baseline_articles': g['N'].get('baseline_article_count', 0),
                        'P': g['P']['value'], 'P_status': g['P']['status'],
                        'P_hold_reason': g['P']['hold_reason'],
                        'P_diagnostic': g['P'].get('diagnostic_percentile'),
                        'grouping_method': g.get('grouping_method', 'lexical-v1'),
                        'primary_fact': g.get('primary_fact'),
                        'P_baseline_groups': g['P'].get('baseline_group_count', 0),
                        'observation_seconds': g['P'].get('observation_seconds')}
                       for g in result['groups']]}


def compare_memberships(default, scenarios):
    checks = []
    for group in default['groups']:
        urls = set(group.get('member_keys',group['member_urls']))
        comparisons = []
        for scenario in scenarios:
            matched = [g for g in scenario['result']['groups'] if urls & set(g.get('member_keys',g['member_urls']))]
            comparisons.append({'group_similarity_cut': scenario['group_similarity_cut'],
                                'p_observation_max_hours': scenario['p_observation_max_hours'],
                                'same_membership': len(matched) == 1 and set(matched[0].get('member_keys',matched[0]['member_urls'])) == urls,
                                'matched_group_ids': [g['group_id'] for g in matched],
                                'N_values': [g['N'] for g in matched], 'P_values': [g['P'] for g in matched]})
        values = [v for c in comparisons for v in c['P_values'] if v is not None]
        checks.append({'group_id': group['group_id'], 'title': group['title'],
                       'stable_membership': all(c['same_membership'] for c in comparisons),
                       'P_range_across_matched_groups': [min(values), max(values)] if values else None,
                       'comparisons': comparisons})
    return checks


def validate_case(report, rows, config, cuts, hours):
    if not cuts or any(isinstance(c, bool) or not isinstance(c, (int,float)) or not 0 < c <= 1 for c in cuts):
        raise ValueError('민감도 유사도 기준은 0 초과 1 이하여야 합니다')
    if not hours or any(isinstance(h, bool) or not isinstance(h, (int,float)) or not 0 < h <= review.GROUP_HOURS for h in hours):
        raise ValueError('민감도 관찰시간은 0 초과 72시간 이하여야 합니다')
    default = np.score(report, rows, config)
    brief = compact(default)
    scenarios = []
    if default['status'] in ('computed', 'partial'):
        for cut in cuts:
            for horizon in hours:
                result = np.score(report, rows, {**config, 'group_similarity_cut': cut,
                                               'p_observation_max_hours': horizon})
                scenarios.append({'group_similarity_cut': cut, 'p_observation_max_hours': horizon,
                                  'result': compact(result)})
    checks = compare_memberships(brief, scenarios) if scenarios else []
    return {'drop_id': report['drop_id'], 'coverage': report['coverage'],
            'input_sha256': default.get('input_sha256'), 'default': brief,
            'scenarios': scenarios, 'membership_checks': checks,
            'sensitivity_status': 'not_evaluated' if not scenarios else
                ('membership_changed' if any(not c['stable_membership'] for c in checks) else 'membership_unchanged')}, default


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--drop-ids', nargs='+', default=DEFAULT_CASES)
    ap.add_argument('--cache', type=Path, default=HERE / 'cache/naver')
    ap.add_argument('--config', type=Path, default=np.CONFIG_PATH)
    ap.add_argument('--similarity-cuts', type=float, nargs='+', default=[0.35, 0.42, 0.50])
    ap.add_argument('--observation-hours', type=float, nargs='+', default=[24, 48, 72])
    args = ap.parse_args(argv)
    config = json.loads(args.config.read_text(encoding='utf-8'))
    # 모두 먼저 읽어서 잘못된 사건 ID 때문에 일부 결과만 저장되는 것을 피한다.
    try:
        reports = [review.build_report(drop, args.cache) for drop in dict.fromkeys(args.drop_ids)]
        cases, defaults = [], []
        for report, rows in reports:
            case, default = validate_case(report, rows, config, sorted(set(args.similarity_cuts)),
                                          sorted(set(args.observation_hours)))
            cases.append(case)
            defaults.append(default)
    except (ValueError, KeyError) as exc:
        ap.error(str(exc))
    output = {'rule_version': defaults[0]['rule_version'] if defaults else np.RULE_VERSION, 'default_config': config, 'cases': cases,
              'limits': ['설정 민감도와 실행 가능성 확인이며 수익률 예측력·급락 원인 검증은 아님',
                         '같은 회사의 겹치는 기준 기간은 독립 표본이 아님',
                         '설정별 묶음 분할·병합이 있으면 지표 값의 일대일 비교도 주의해야 함',
                         '이 결과로 기본 설정을 자동 최적화하거나 위험 임계값을 정하지 않음']}
    scores = args.cache / 'scores'
    scores.mkdir(exist_ok=True)
    for default in defaults:
        (scores / f"{default['drop_id']}.json").write_text(json.dumps(default, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    directory = args.cache / 'validation'
    directory.mkdir(exist_ok=True)
    path = directory / 'np_validation.json'
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps({'cases': [{'drop_id': c['drop_id'], 'status': c['default']['status'],
                                'group_count': c['default']['group_count'], 'sensitivity': c['sensitivity_status'],
                                'changed_memberships': sum(not m['stable_membership'] for m in c['membership_checks'])}
                               for c in cases], 'saved_to': str(path)}, ensure_ascii=False, indent=2))
    return 0 if all(c['default']['status'] == 'computed' for c in cases) else 1


if __name__ == '__main__':
    raise SystemExit(main())
