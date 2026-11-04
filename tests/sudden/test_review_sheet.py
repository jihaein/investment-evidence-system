"""검수표 생성·덮어쓰기 방지·집계를 검증한다."""
import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'models/sudden'))
import review_sheet as rs


def annotations(tmp_path):
    path = tmp_path / 'a.json'
    path.write_text(json.dumps({'articles': [
        {'url': 'u1', 'case_id': 'c', 'has_primary_fact': True, 'absent_fields': ['contract_start'],
         'expected': {'actor': 'DMS', 'fields.amount_krw': '6100000000'}, 'note': 'n'},
        {'url': 'u2', 'case_id': 'c', 'has_primary_fact': False}]}, ensure_ascii=False), encoding='utf-8')
    return path


def fill(sheet, verdicts):
    with sheet.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    for row, (verdict, wrong) in zip(rows, verdicts):
        row.update(판정=verdict, 틀린_필드=wrong, 검수자='팀원')
    with sheet.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def test_sheet_shows_ai_values_and_keeps_human_edits(tmp_path):
    sheet = tmp_path / 's.csv'
    assert rs.write_sheet(annotations(tmp_path), sheet)
    with sheet.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    assert (rows[0]['AI_주체'], rows[0]['AI_금액_원'], rows[0]['AI_없다고_본_필드']) == ('DMS', '6100000000', 'contract_start')
    assert rows[1]['AI_주사실'] == '없음' and rows[1]['판정'] == ''
    fill(sheet, [('맞음', '')])
    assert not rs.write_sheet(annotations(tmp_path), sheet)
    assert rs.compare(sheet)['reviewed'] == 1


def test_compare_counts_agreement_without_unknown(tmp_path):
    sheet = tmp_path / 's.csv'
    rs.write_sheet(annotations(tmp_path), sheet)
    fill(sheet, [('틀림', 'AI_금액_원'), ('모름', '')])
    result = rs.compare(sheet)
    assert result['agreement'] == 0.0 and result['verdicts'] == {'틀림': 1, '모름': 1}
    assert result['wrong_fields'] == {'AI_금액_원': 1}
    fill(sheet, [('맞아요', '')])
    with pytest.raises(SystemExit, match='판정 칸'):
        rs.compare(sheet)
