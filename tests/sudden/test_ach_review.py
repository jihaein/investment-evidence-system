import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import ach_review as ar  # noqa: E402


def _case(drop_id, verdict, types, reason=None, name='가나'):
    return {'drop_id': drop_id, 'stock_code': '017800', 'company_name': name, 'D': '2023-05-01', 'z': -3.0,
            'verdict': verdict, 'hypothesis': 'H1', 'reason': reason, 'flags': {'fragile': True, 'mixed': []},
            'support': {'H1': 1.0}, 'refute': {'H3': 1.0},
            'selected_docs': [{'bundle_id': 'b1', 'title': '제목', 'url': 'u', 'published_at': '2023-05-01T09:00:00+09:00', 'S_sel': 50}],
            'units': [{'unit_id': f'u{i}', 'type': t, 'direction': '부정', 'quote': 'q', 'event': 'e', 'w': 0.5,
                       'members': [{'bundle_id': 'b1', 'url': 'u'}], 'mandatory': t == 'C2'} for i, t in enumerate(types)]}


def test_units_skip_rule_made_and_spread_types():
    cases = [_case('a', '가치훼손', ['F1', 'F1', 'F1', 'S2', 'K4', 'X0', 'C2'])]
    units = ar.reviewable_units(cases)
    assert sorted(u['ai_type'] for u in units) == ['F1', 'F1', 'F1', 'S2']     # K·X0·필수 공시 제외
    picked = ar.sample_units(units, 2, seed=1)
    assert sorted(u['ai_type'] for u in picked) == ['F1', 'S2']               # 유형별로 돌아가며


def test_cases_balance_verdicts_and_skip_no_evidence():
    cases = [_case(f'v{i}', '가치훼손', ['F1']) for i in range(3)] + [_case(f'n{i}', '비가치', ['S2']) for i in range(3)] + \
            [_case('h0', '판단보류', [], 'no_evidence_in_window'), _case('h1', '판단보류', ['F1'], 'weak_support')]
    picked = ar.sample_cases(cases, 4, seed=1)
    assert sorted(c['ai_verdict'] for c in picked) == ['가치훼손', '가치훼손', '비가치', '판단보류']
    assert 'h0' not in {c['drop_id'] for c in picked} and picked[0]['ai_flags'] == {'fragile': True}


def test_company_name_uses_fixed_names_table(tmp_path):
    names = tmp_path / 'stock_names.csv'
    names.write_text('stock_code,company_name,from,to\n017800,현대엘리베이터,2022-01-01,2025-12-31\n', encoding='utf-8-sig')
    ach_dir = tmp_path / 'ach'
    ach_dir.mkdir()
    (ach_dir / '017800_20230501.json').write_text(json.dumps(_case('017800_20230501', '비가치', ['F1'], name='현대엘리베이'),
                                                             ensure_ascii=False), encoding='utf-8')
    case = _case('017800_20230501', '비가치', ['F1'], name='현대엘리베이')
    case['units'][0]['event'] = '현대엘리베이-회사채 발행'
    (ach_dir / '017800_20230501.json').write_text(json.dumps(case, ensure_ascii=False), encoding='utf-8')
    data = ar.build(ach_dir, None, 5, 5, 1, names_path=names)
    assert data['units'][0]['company'] == '현대엘리베이터' and data['cases'][0]['company'] == '현대엘리베이터'
    assert data['units'][0]['event'] == '현대엘리베이터-회사채 발행' and data['cases'][0]['units'][0]['event'] == '현대엘리베이터-회사채 발행'
    assert ar.fix_name('현대엘리베이터 주가', '현대엘리베이', '현대엘리베이터') == '현대엘리베이터 주가'   # 이미 맞는 이름은 그대로
    assert data['types']['F1'][0] == '실적·전망 하향' and len(data['types']['F1']) == 2   # 이름과 예시


def test_compare_multi_types_and_blind_answers():
    rows = [{'kind': 'unit', 'about_company': '예', 'types': 'F1', 'first_types': 'F1', 'ai_type': 'F1', 'direction': '부정', 'ai_direction': '부정'},
            {'kind': 'unit', 'about_company': '예', 'types': 'F2|F3', 'first_types': 'F3', 'ai_type': 'F2', 'direction': '부정',
             'ai_direction': '긍정', 'changed_after_reveal': 'Y'},
            {'kind': 'unit', 'about_company': '예', 'types': 'C1', 'first_types': 'C1', 'ai_type': 'S2', 'direction': '부정', 'ai_direction': '부정'},
            {'kind': 'unit', 'about_company': '아니오', 'types': 'X0', 'ai_type': 'F2', 'direction': '없음', 'ai_direction': '긍정'},
            {'kind': 'case', 'verdict': '판단보류', 'first_verdict': '판단보류', 'hypotheses': '', 'ai_verdict': '비가치', 'ai_hypothesis': 'H3'},
            {'kind': 'case', 'verdict': '비가치', 'first_verdict': '가치훼손', 'hypotheses': 'H3|H4', 'ai_verdict': '비가치', 'ai_hypothesis': 'H4'}]
    r = ar.compare(rows)
    assert r['units_reviewed'] == 4 and r['not_about_company'] == 1
    assert r['type_agree'] == 0.667 and r['type_agree_blind'] == 0.333 and r['multi_type_share'] == 0.333
    assert r['direction_agree'] == 0.667 and r['type_misses_ai_to_human'] == ['S2→C1 1'] and r['changed_after_reveal'] == 1
    assert r['verdict_agree'] == 0.5 and r['verdict_agree_blind'] == 0.0 and r['hypothesis_agree'] == 1.0


def test_html_embeds_data_safely(tmp_path):
    data = {'source': 's', 'seed': 1, 'types': {'F1': ['실적', '예']}, 'units': [{'quote': '</script><b>'}], 'cases': []}
    ar.write_html(data, tmp_path / 'r.html')
    html = (tmp_path / 'r.html').read_text(encoding='utf-8')
    assert '__DATA__' not in html and '<\\/script>' in html and '실적' in html
