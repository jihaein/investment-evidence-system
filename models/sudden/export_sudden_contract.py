"""급락 모델 결과를 계약 파일(JSON)로 내보낸다. 형식은 contracts/sudden_result.schema.json.

백테스트·대시보드는 이 파일만 읽고 판정을 다시 계산하지 않는다 (서서히 모델의 export_contract.py와 같은 방식).
- 판정 정보 마감은 급락일 장 마감(decision_time)이다. 매매는 그 다음 거래일부터 한다.
- 판단보류는 hold_reason에 사유를 남긴다. H0(가설 없음)은 hypothesis=null로 쓴다.
- 기사 발췌는 넣지 않는다. 뉴스 단위는 유형·방향·가중치·URL만, 공시·시장 규칙 단위는 공시 제목·수치를 넣는다.

    python models/sudden/export_sudden_contract.py                     # 2015~2019 표본 → contracts/sudden_result.json
    python models/sudden/export_sudden_contract.py --sample 2023_tune  # → contracts/sudden_result_2023_tune.json
"""
import argparse
import csv
import json
import subprocess
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SCHEMA_VERSION = '0.1'
HYPS = ('H1', 'H2', 'H3', 'H4', 'H5')
SAMPLES = {
    '2015': {'out': 'output', 'ach': 'cache/naver_full/ach_ms', 'split': None, 'file': 'sudden_result.json'},
    '2023_tune': {'out': 'output_2023', 'ach': 'cache/naver_2023/ach_ms', 'split': 'tune', 'file': 'sudden_result_2023_tune.json'},
}
PARAMS = {'gap': 0.5, 'positive_weight': 1.0, 'market_only_holds': True, 'positive_supports': True}
NOTES = [
    'V=1, N·P는 사실 기반 임시 지표',
    '시장 규칙은 K4만, 업종 트랙 없음',
    '실적 공시 방향은 전년 대비(기대치 대비 아님)',
    '사람 검수 전: 개별 판정을 정답으로 쓰지 않는다',
]


def git_info():
    def run(*args):
        try:
            return subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            return ''
    return run('rev-parse', 'HEAD') or None, bool(run('status', '--porcelain', '--', 'models/sudden'))


def source_of(u):
    if u.get('market_rule'):
        return 'market'
    if any(str(m.get('bundle_id', '')).startswith('dart:') for m in u.get('members', [])):
        return 'dart'
    return 'news'


def unit_out(u):
    src = source_of(u)
    urls = sorted({m['url'] for m in u.get('members', []) if m.get('url')})
    return {'unit_id': u['unit_id'], 'source': src, 'type': u['type'], 'direction': u['direction'],
            'weight': round(float(u['w']), 4), 'mandatory': bool(u.get('mandatory')), 'certainty': u.get('certainty'),
            'label': u.get('event'), 'filing': None if src == 'news' else u.get('quote'), 'urls': urls}


def hyp(h):
    return h if h in HYPS else None


def case_out(r, event):
    zero = {h: 0.0 for h in HYPS}
    held = r['verdict'] == '판단보류'
    inputs = r.get('inputs') or {}
    flags = r.get('flags') or {}
    as_int = lambda v: v if isinstance(v, int) else None
    return {'drop_id': r['drop_id'], 'stock_code': r['stock_code'], 'company_name': r['company_name'],
            'market': event.get('market'), 'drop_date': r['D'], 'decision_time': event.get('decision_time') or f"{r['D']}T15:30:00+09:00",
            'z': float(r['z']) if r.get('z') is not None else None,
            'ar_pct': float(event['ar_pct']) if event.get('ar_pct') else None,
            'verdict': r['verdict'], 'hypothesis': hyp(r.get('hypothesis')),
            'hold_reason': (r.get('reason') or 'unspecified') if held else None, 'runner_up': hyp(r.get('runner_up')),
            'scores': {'support': {**zero, **{k: round(v, 4) for k, v in (r.get('support') or {}).items()}},
                       'refute': {**zero, **{k: round(v, 4) for k, v in (r.get('refute') or {}).items()}}},
            'flags': {'fragile': bool(flags.get('fragile')), 'unverified_basis': bool(flags.get('unverified_basis')),
                      'mixed': list(flags.get('mixed') or []), 'low_confidence': bool(flags.get('low_confidence')),
                      **({'doc_fragile': bool(flags['doc_fragile'])} if 'doc_fragile' in flags else {})},
            'inputs': {'news_docs': int(inputs.get('news_docs') or 0), 'dart_units': as_int(inputs.get('dart')),
                       'market_units': as_int(inputs.get('market_rules'))},
            'evidence': [unit_out(u) for u in r.get('units') or []]}


def build(sample, cfg=None):
    """cfg: {'out': 사건표 폴더, 'ach': ACH 결과 폴더, 'split': ...}. 없으면 SAMPLES[sample] (HERE 기준 상대 경로도 된다)."""
    cfg = cfg or SAMPLES[sample]
    with (HERE / cfg['out'] / 'drop_events.csv').open(encoding='utf-8-sig', newline='') as f:
        events = {e['drop_id']: e for e in csv.DictReader(f)}
    results = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((HERE / cfg['ach']).glob('[0-9]*_[0-9]*.json'))]
    cases = [case_out(r, events.get(r['drop_id'], {})) for r in results]
    commit, dirty = git_info()
    first = next((r for r in results if r.get('rule_version')), {})
    dates = sorted(c['drop_date'] for c in cases)
    summary = HERE / cfg['ach'] / 'summary.json'
    params = json.loads(summary.read_text(encoding='utf-8')).get('params') if summary.exists() else None
    return {'schema_version': SCHEMA_VERSION, 'generated_at': datetime.now().isoformat(timespec='seconds'),
            'source_commit': commit, 'source_dirty': dirty, 'rule_version': first.get('rule_version', 'unknown'),
            'prompt_version': first.get('prompt_version'), 'classifier_model': first.get('model'), 'params': params or PARAMS,
            'sample': {'name': sample, 'split': cfg['split'], 'first_drop_date': dates[0] if dates else None,
                       'last_drop_date': dates[-1] if dates else None},
            'notes': NOTES, 'cases': cases}


def sell_schedule(contract, verdicts=('가치훼손',), skip_fragile=False):
    """백테스트용: 종목마다 조건에 맞는 첫 급락일 → {stock_code: drop_date}. 매도일(다음 거래일)은 백테스트가 정한다."""
    out = {}
    for c in sorted(contract['cases'], key=lambda c: c['drop_date']):
        if c['verdict'] in verdicts and not (skip_fragile and (c['flags']['fragile'] or c['flags'].get('doc_fragile'))):
            out.setdefault(c['stock_code'], c['drop_date'])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sample', choices=sorted(SAMPLES), default='2015')
    ap.add_argument('--out', type=Path)
    ap.add_argument('--ach', type=Path, help='ACH 결과 폴더 (live.py 등, --events와 함께)')
    ap.add_argument('--events', type=Path, help='drop_events.csv가 있는 폴더')
    ap.add_argument('--name', help='--ach를 쓸 때 표본 이름')
    args = ap.parse_args(argv)
    if args.ach:
        name = args.name or args.ach.name
        contract = build(name, {'out': args.events, 'ach': args.ach, 'split': None})
        out = args.out or ROOT / 'contracts' / f'sudden_result_{name}.json'
    else:
        contract = build(args.sample)
        out = args.out or ROOT / 'contracts' / SAMPLES[args.sample]['file']
    out.write_text(json.dumps(contract, ensure_ascii=False, indent=1), encoding='utf-8')
    counts = {v: sum(c['verdict'] == v for c in contract['cases']) for v in ('가치훼손', '비가치', '판단보류')}
    print(json.dumps({'out': str(out), 'cases': len(contract['cases']), **counts}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
