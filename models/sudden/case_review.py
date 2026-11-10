"""공개 사례 점검표 (설계서 v3.1 7절): 판정과 근거 단위를 사람이 읽고 명백한 오류를 찾기 위한 표를 만든다.

정답셋이 아니다. "유상증자 급락을 H3로 판정" 같은 명백한 오류를 찾는 용도다.
출력에는 기사 인용이 들어가므로 cache 폴더에만 저장한다(저장소에 올리지 않는다).

    python models/sudden/case_review.py                       # 기본 12건
    python models/sudden/case_review.py --drop-id 010170_20180228 --drop-id ...

출력: <ach 폴더>/case_review_events.csv (사건별 판정 확인), case_review_units.csv (단위별 유형·방향 확인)
"""
import argparse
import csv
import json
from pathlib import Path

import ach

HERE = Path(__file__).resolve().parent
# 판정별로 원인이 비교적 분명한 사건(공시·기사 근거가 있는 것)과, 오류가 의심되는 유형(주가 서술, K4 단독)을 섞었다
DEFAULT_CASES = [
    ('010170_20180228', '가치훼손', '유상증자 결정 공시'),
    ('050540_20181219', '가치훼손', 'CB·유상증자 결정 + 경영권 분쟁'),
    ('052270_20160810', '가치훼손', '중국 자회사 관련 기사 다수'),
    ('089010_20180227', '가치훼손', '영업손실 적자전환 기사'),
    ('053660_20180927', '가치훼손', '기사 없이 유상증자 공시만'),
    ('036890_20161128', '비가치', 'CB 전환청구 물량(S2)'),
    ('083650_20170711', '비가치', '공급계약 공시 + 대주주 지분 매도'),
    ('068790_20171211', '비가치', '공급계약 체결 공시 뒤 급락'),
    ('097800_20170816', '비가치', '풍문 해명 + CB 발행 + 거래정지'),
    ('153460_20180914', '비가치', '기사 없이 K4(급락 전 과열)만'),
    ('093190_20171101', '판단보류', '최대주주 지분 양도'),
    ('036890_20161214', '가치훼손', '오류 의심: 주가 서술이 M3·F3로 분류'),
]
REVIEW_EVENT = ['verdict_ok(O/X/?)', 'correct_hypothesis', 'obvious_error', 'note']
REVIEW_UNIT = ['type_ok(O/X)', 'human_type', 'direction_ok(O/X)', 'note']


def rows_for(result, why):
    support, refute = result.get('support', {}), result.get('refute', {})
    flags = [k for k, v in (result.get('flags') or {}).items() if v]
    event = {'drop_id': result['drop_id'], 'company': result['company_name'], 'D': result['D'], 'z': result['z'],
             'why_picked': why, 'verdict': result['verdict'], 'hypothesis': result['hypothesis'],
             'runner_up': result.get('runner_up'), 'reason': result.get('reason'),
             'support': ' '.join(f'{h}={support[h]:.2f}' for h in ach.HYPOTHESES if support.get(h)),
             'refute': ' '.join(f'{h}={refute[h]:.2f}' for h in ach.HYPOTHESES if refute.get(h)),
             'flags': ','.join(flags), 'news_docs': result.get('inputs', {}).get('news_docs'),
             'dart_units': result.get('inputs', {}).get('dart'), 'market_units': result.get('inputs', {}).get('market_rules'),
             **{k: '' for k in REVIEW_EVENT}}
    units = []
    for u in sorted(result.get('units', []), key=lambda u: -u['w']):
        rel = {h: ach.relation(u['type'], u['direction'], h) for h in ach.HYPOTHESES}
        units.append({'drop_id': result['drop_id'], 'company': result['company_name'], 'unit_id': u['unit_id'],
                      'type': u['type'], 'type_name': ach.TYPES.get(u['type'], ('시장 규칙',))[0],
                      'direction': u['direction'], 'w': u['w'], 'certainty': u.get('certainty'),
                      'source': 'DART' if u.get('mandatory') else ('시장 규칙' if u.get('market_rule') else '기사'),
                      'supports': ''.join(h for h, r in rel.items() if r == '+'),
                      'refutes': ''.join(h for h, r in rel.items() if r == '-'),
                      'event': u['event'], 'quote': u['quote'],
                      'url': (u.get('members') or [{}])[0].get('url', ''), **{k: '' for k in REVIEW_UNIT}})
    return event, units


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ach', type=Path, default=HERE / 'cache/naver_full/ach')
    ap.add_argument('--drop-id', action='append')
    args = ap.parse_args(argv)
    cases = [(d, '', '직접 지정') for d in args.drop_id] if args.drop_id else DEFAULT_CASES
    events, units = [], []
    for drop_id, _, why in cases:
        result = json.loads((args.ach / f'{drop_id}.json').read_text(encoding='utf-8'))
        e, us = rows_for(result, why)
        events.append(e)
        units += us
    for name, rows in (('case_review_events.csv', events), ('case_review_units.csv', units)):
        with (args.ach / name).open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    print(f'사건 {len(events)}건, 단위 {len(units)}개 → {args.ach / "case_review_events.csv"}, {args.ach / "case_review_units.csv"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
