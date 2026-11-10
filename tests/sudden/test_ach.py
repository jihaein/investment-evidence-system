"""ACH 판정: 관계표·판정 규칙·확실성·다수결을 외부 접속 없이 확인한다. 설계서 5.7 계산 예시를 그대로 재현한다."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import ach


def unit(t, w, direction='부정', **kw):
    return {'type': t, 'direction': direction, 'w': w, 'unverified': False, 'ambiguous': False,
            'run_types': [t] * 3, **kw}


def design_example():
    return [unit('F2', 1.00, mandatory=True), unit('R1', 0.31), unit('K1b', 0.60, direction='없음'),
            unit('S2', 0.16, unverified=True)]


def test_design_example_5_7_adopts_h1_without_flags():
    main, flags = ach.judge(design_example())
    assert main['support'] == {'H1': 1.31, 'H2': 0.31, 'H3': 0, 'H4': 0.16, 'H5': 0}
    assert main['refute'] == {'H1': 0, 'H2': 0, 'H3': 1.6, 'H4': 0, 'H5': 1.0}
    assert main['hypothesis'] == 'H1' and main['verdict'] == '가치훼손'
    assert flags['fragile'] is False and flags['mixed'] == [] and flags['unverified_basis'] is False


def test_removing_mandatory_disclosure_is_not_the_sensitivity_check():
    units = design_example()
    units[0]['mandatory'] = False                        # 공시가 아니라면 u1을 빼고 다시 판정 → H1·H2 vs H4 차이 0.15 → 보류
    assert ach.judge(units)[1]['fragile'] is True


def test_positive_news_flips_relation_but_r2_does_not():
    assert ach.relation('F1', '부정', 'H1') == '+' and ach.relation('F1', '긍정', 'H1') == '-'
    assert ach.relation('F1', '긍정', 'H3') == '+' and ach.relation('F7', '긍정', 'H3') == '0'
    assert ach.relation('R2', '긍정', 'H5') == '+' and ach.relation('R2', '부정', 'H5') == '+'


def test_no_support_is_h0_and_close_groups_are_held():
    assert ach.decide([unit('X0', 1.0)])['hypothesis'] == 'H0'
    held = ach.decide([unit('F7', 0.5), unit('S2', 0.4)])         # H1 0.5 vs H4 0.4, 둘 다 반증 0
    assert held['verdict'] == '판단보류' and held['reason'] == 'competing_groups_too_close'
    same_group = ach.decide([unit('F7', 0.5), unit('C1', 0.4)])  # H1·H2 둘 다 가치훼손이면 그 묶음으로 판정
    assert same_group['verdict'] == '가치훼손'


def test_mixed_flag_when_other_group_has_no_refutation():
    main, flags = ach.judge([unit('F7', 1.0), unit('S2', 0.45)])
    assert main['hypothesis'] == 'H1' and flags['mixed'] == ['H4']


def test_certainty_rules():
    assert ach.certainty('매각설이 돌았다', '시장에서는 대주주 매각설이 돌았다.')[1] == '미확인'
    assert ach.certainty('실적이 줄어들 전망', '올해 실적이 줄어들 전망이다.')[1] == '추정'
    assert ach.certainty('증설하기로 했다', '공장을 증설하기로 했다.')[1] == '예정'
    assert ach.certainty('공급계약을 해지', 'B사와의 공급계약을 해지했다고 공시했다.')[1] == '확정'
    assert ach.certainty('건설 수주', '건설 수주를 따냈다.')[1] == '확정'


def test_majority_keeps_agreed_units_and_marks_three_way_disagreement():
    text = 'A사는 B사와의 공급계약을 해지했다. 시장에서는 대주주 블록딜 추진설이 돌았다.'
    q1, q2 = 'B사와의 공급계약을 해지했다', '대주주 블록딜 추진설이 돌았다'
    runs = [[{'quote': q1, 'type': 'F2', 'direction': '부정', 'event': 'e'}, {'quote': q2, 'type': 'S2', 'direction': '부정', 'event': 'f'}],
            [{'quote': q1, 'type': 'F2', 'direction': '부정', 'event': 'e'}, {'quote': q2, 'type': 'S1', 'direction': '부정', 'event': 'f'}],
            [{'quote': q1, 'type': 'F1', 'direction': '부정', 'event': 'e'}, {'quote': q2, 'type': 'M3', 'direction': '부정', 'event': 'f'},
             {'quote': '지어낸 문장', 'type': 'C1', 'direction': '부정', 'event': 'g'}]]
    units, stats = ach.majority(runs, text)
    by_quote = {u['quote']: u for u in units}
    assert by_quote[q1]['type'] == 'F2' and not by_quote[q1]['ambiguous']
    assert by_quote[q2]['ambiguous'] and by_quote[q2]['type'] == 'S2' and by_quote[q2]['run_types'] == ['S2', 'S1', 'M3']
    assert stats == {'units_proposed': 7, 'units_quote_ok': 6} and len(units) == 2


def test_selection_ranks_by_s_sel_and_fills_low_confidence():
    sessions = [{'d': '0', 'session_start': '2018-01-04T15:30:00+09:00', 'session_end': '2018-01-05T15:30:00+09:00'},
                {'d': '1', 'session_start': '2018-01-03T15:30:00+09:00', 'session_end': '2018-01-04T15:30:00+09:00'}]
    rows = {f'u{i}': {'url': f'u{i}', 'title': 't', 'body': '본문', 'published_at': '2018-01-04T10:00:00+09:00'} for i in range(3)}
    groups = [{'group_id': f'g{i}', 'representative_url': f'u{i}', 'first_published_at': '2018-01-04T10:00:00+09:00',
               'members': [{'publisher': 'A', 'published_at': '2018-01-04T10:00:00+09:00'}],
               'N': {'value': n}, 'P': {'value': p}} for i, (n, p) in enumerate([(1.0, 1.0), (None, None), (0.0, 0.0)])]
    docs = ach.selection(groups, rows, sessions)
    assert [d['bundle_id'] for d in docs] == ['g0', 'g1', 'g2']
    assert docs[0]['S_sel'] == 90.0 and docs[1]['n_missing'] and docs[2]['S_sel'] == 10.0
    assert docs[2]['low_confidence'] and not docs[0]['low_confidence']


def test_classifier_caches_each_run(tmp_path):
    calls = []

    def create(**kw):
        calls.append(kw)
        msg = SimpleNamespace(content='{"units": []}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    c = ach.Classifier('gpt-5-nano', tmp_path, client)
    doc = {'url': 'u', 'title': 't', 'text': 'x', 'published_at': '2018-01-04T10:00:00+09:00'}
    for r in (0, 1, 0):
        c(doc, 'A사', r)
    assert len(calls) == 2 and calls[0]['reasoning_effort'] == 'minimal' and 'temperature' not in calls[0]


def test_mandatory_disclosure_absorbs_same_type_news_and_is_not_removed():
    news = {'type': 'F2', 'direction': '부정', 'quote': '기사 문장', 'event': 'A사 계약 해지 보도', 'w_doc': 0.5, 'a_u': 1.0,
            'g_u': 1.0, 'r_cert': 1.0, 'certainty': '확정', 'ambiguous': False, 'run_types': ['F2'] * 3,
            'bundle_id': 'b1', 'url': 'u1'}
    filing = {**news, 'quote': '단일판매ㆍ공급계약해지', 'event': 'A사-단일판매ㆍ공급계약해지', 'w_doc': 1.0,
              'mandatory': True, 'bundle_id': 'dart:1', 'url': 'd1'}
    units = ach.merge([news, filing])
    assert len(units) == 1 and units[0]['mandatory'] and units[0]['w'] == 1.0 and len(units[0]['members']) == 2
    main, flags = ach.judge(units)
    assert main['hypothesis'] == 'H1' and flags['fragile'] is False


def test_market_rule_k4_supports_h3_h4_without_direction():
    assert ach.relation('K4', '없음', 'H3') == '+' and ach.relation('K4', '긍정', 'H4') == '+'
    assert ach.relation('K4', '없음', 'H1') == '0'


def test_params_market_only_holds_and_positive_weight():
    k4 = unit('K4', 0.6, direction='없음', market_rule=True)
    assert ach.decide([k4])['verdict'] == '비가치'
    held = ach.decide([k4], params={'market_only_holds': True})
    assert held['verdict'] == '판단보류' and held['reason'] == 'market_rule_only'
    assert ach.decide([k4, unit('C1', 1.2)], params={'market_only_holds': True})['verdict'] == '가치훼손'   # 기사·공시 근거가 있으면 판정
    good = unit('F2', 1.0, direction='긍정')                       # 좋은 소식 + 급락 → H3 지지
    assert ach.decide([good])['hypothesis'] == 'H3'
    assert ach.decide([good], params={'positive_weight': 0.0})['hypothesis'] == 'H0'
