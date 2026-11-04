"""AI 보조 검토 라벨을 사람이 확인하는 검수표를 만들고, 채운 검수표의 일치율을 센다.

검수자는 기사 URL을 열어 AI_ 칸의 값이 원문과 맞는지 판정한다.
AI 값을 보고 판정하므로 독립 라벨링보다 일치율이 높게 나올 수 있다(확인 편향). 결과를 보고할 때 같이 적는다.

[실행]
    python models/sudden/review_sheet.py                         # eval/human_review_v3.csv 생성 (이미 있으면 그대로 둠)
    python models/sudden/review_sheet.py --compare               # 채운 검수표 집계
"""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVAL_DIR = HERE / 'eval'
FIELDS = [('actor', '주체'), ('action', '행동'), ('stage', '단계'), ('target', '대상'),
          ('fields.amount_krw', '금액_원'), ('fields.currency', '통화'), ('fields.counterparty', '상대방'),
          ('fields.contract_start', '계약시작'), ('fields.contract_end', '계약종료'),
          ('fields.delivery_region', '공급지역')]
REVIEW_COLUMNS = ['검수자', '판정', '틀린_필드', '올바른_값', '검수_메모']
VERDICTS = ('맞음', '틀림', '모름')


def rows_from(annotations):
    rows = []
    for i, a in enumerate(annotations['articles'], 1):
        expected = a.get('expected') or {}
        row = {'번호': i, 'case_id': a['case_id'], '기사_URL': a['url'],
               'AI_주사실': '있음' if a['has_primary_fact'] else '없음'}
        for key, label in FIELDS:
            row[f'AI_{label}'] = expected.get(key, '')
        row['AI_허용값'] = '; '.join(f"{k}={'|'.join(v)}" for k, v in (a.get('accepted_values') or {}).items())
        row['AI_없다고_본_필드'] = '|'.join(a.get('absent_fields', []))
        row['AI_메모'] = a.get('note', '')
        row.update({c: '' for c in REVIEW_COLUMNS})
        rows.append(row)
    return rows


def write_sheet(annotations_path, out):
    if out.exists():
        return False                                         # 사람이 채운 내용을 지키기 위해 덮어쓰지 않는다
    rows = rows_from(json.loads(annotations_path.read_text(encoding='utf-8')))
    with out.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return True


def compare(sheet):
    with sheet.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    done = [r for r in rows if r['판정'].strip()]
    bad = [r for r in done if r['판정'].strip() not in VERDICTS]
    if bad:
        raise SystemExit(f"판정 칸은 {'/'.join(VERDICTS)} 중 하나여야 합니다: 번호 {[r['번호'] for r in bad]}")
    verdicts = Counter(r['판정'].strip() for r in done)
    judged = verdicts['맞음'] + verdicts['틀림']
    wrong_fields = Counter(f.strip() for r in done if r['판정'].strip() == '틀림'
                           for f in r['틀린_필드'].split('|') if f.strip())
    return {'rows': len(rows), 'reviewed': len(done), 'verdicts': dict(verdicts),
            'agreement': round(verdicts['맞음'] / judged, 3) if judged else None,
            'reviewers': sorted({r['검수자'].strip() for r in done if r['검수자'].strip()}),
            'wrong_fields': dict(wrong_fields),
            'wrong_rows': [{'번호': r['번호'], 'url': r['기사_URL'], '틀린_필드': r['틀린_필드'],
                            '올바른_값': r['올바른_값'], '메모': r['검수_메모']}
                           for r in done if r['판정'].strip() == '틀림'],
            'limits': ['AI 값을 보고 판정한 확인 검수라 독립 라벨보다 일치율이 높게 나올 수 있음',
                       '모름은 일치율 계산에서 뺌']}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--annotations', type=Path, default=EVAL_DIR / 'annotations_v3.json')
    ap.add_argument('--sheet', type=Path, default=EVAL_DIR / 'human_review_v3.csv')
    ap.add_argument('--compare', action='store_true', help='채운 검수표 집계')
    args = ap.parse_args(argv)
    if args.compare:
        print(json.dumps(compare(args.sheet), ensure_ascii=False, indent=2))
    else:
        wrote = write_sheet(args.annotations, args.sheet)
        print('검수표:', args.sheet, '(새로 만듦)' if wrote else '(이미 있어서 그대로 둠)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
