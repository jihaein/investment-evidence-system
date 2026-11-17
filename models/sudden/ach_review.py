"""ACH 판정 사람 검수: 검수 화면(HTML)을 만들고, 내려받은 검수 결과(CSV)를 집계한다.

두 가지를 검수한다.
  - 단위: LLM이 기사 문장에 붙인 유형·방향이 맞는가. 유형은 여러 개 고를 수 있다.
    "대상 회사 이야기인가"도 묻는다(동명·유사 회사 기사 섞임 확인).
  - 사례: 선정 기사 제목·원문을 보고 사람이 판정(가치훼손/비가치/판단보류)과 가설(여러 개 가능)을 고른다.
먼저 사람이 답하고 AI 답을 공개한다. 공개 뒤에도 답을 고칠 수 있고, 공개 전 첫 답(블라인드)은 따로 남긴다.
표본은 고정 seed로 유형별(단위)·판정별(사례)로 고르게 뽑는다. 회사명은 보정된 이름표(stock_names.csv)를 쓴다.

기사 인용·제목이 들어가므로 화면과 결과는 cache/review/ 아래에만 둔다(Git 제외).

    python models/sudden/ach_review.py --ach models/sudden/cache/naver_2023/ach --years 2023,2024
    python models/sudden/ach_review.py --compare models/sudden/cache/review/naver_2023/ach_review_result.csv
"""
import argparse
import csv
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

import ach

HERE = Path(__file__).resolve().parent
OUT_ROOT = HERE / 'cache/review'
EVENT_FILE = re.compile(r'^\d{6}_\d{8}\.json$')
VERDICTS = ('가치훼손', '비가치', '판단보류')


def names_file_for(ach_dir):
    """cache/naver_2023/ach → output_2023/stock_names.csv, cache/naver_full/ach → output/stock_names.csv"""
    tag = Path(ach_dir).parent.name.replace('naver_', '')
    path = HERE / (f'output_{tag}' if tag[:2] == '20' else 'output') / 'stock_names.csv'
    return path if path.exists() else None


def load_names(path):
    """{종목코드: [(from, to, 이름)]}. 없으면 빈 표."""
    out = defaultdict(list)
    if path:
        for r in csv.DictReader(Path(path).open(encoding='utf-8-sig')):
            out[r['stock_code']].append((r['from'], r['to'], r['company_name']))
    return out


def company_name(case, names):
    for start, end, name in names.get(case['stock_code'], []):
        if start <= case['D'] <= end:
            return name
    return case['company_name']


def fix_name(text, old, new):
    """판정 때 쓴 잘린 이름(예: 현대엘리베이)이 LLM 사건 이름에 그대로 들어간 것을 보정 이름으로 바꾼다."""
    if not text or old == new or not new.startswith(old):
        return text
    return re.sub(re.escape(old) + '(?!' + re.escape(new[len(old):]) + ')', new, text)


def load_cases(ach_dir, years=None, names=None):
    cases = []
    for f in sorted(Path(ach_dir).iterdir()):
        if EVENT_FILE.match(f.name):
            d = json.loads(f.read_text(encoding='utf-8'))
            if not years or d['D'][:4] in years:
                d['company'] = company_name(d, names or {})
                for u in d.get('units', []):
                    u['event'] = fix_name(u.get('event', ''), d['company_name'], d['company'])
                cases.append(d)
    return cases


def reviewable_units(cases):
    """LLM이 만든 단위만(필수 공시·시장 규칙·X0 제외)."""
    out = []
    for c in cases:
        titles = {d['bundle_id']: d.get('title', '') for d in c.get('selected_docs', [])}
        for u in c.get('units', []):
            if u.get('mandatory') or u.get('market_rule') or u['type'].startswith('K') or u['type'] == 'X0':
                continue
            m = (u.get('members') or [{}])[0]
            out.append({'drop_id': c['drop_id'], 'company': c.get('company', c['company_name']), 'D': c['D'],
                        'unit_id': u['unit_id'], 'quote': u.get('quote', ''), 'event': u.get('event', ''),
                        'url': m.get('url', ''), 'title': titles.get(m.get('bundle_id'), ''), 'ai_type': u['type'],
                        'ai_direction': u['direction'], 'run_types': u.get('run_types', []),
                        'ver': c.get('prompt_version', '')})
    return out


def _answer(u, ver):
    if u is None:
        return {'ver': ver, 'missing': True}
    return {'ver': ver, 'type': u['ai_type'], 'direction': u['ai_direction'], 'run_types': u['run_types']}


def _match(u, pool, cut=0.6):
    """같은 사건에서 인용이 가장 비슷한 단위(유사도 cut 이상). 없으면 None."""
    same = [v for v in pool if v['drop_id'] == u['drop_id']]
    best = max(same, key=lambda v: ach.sim(v['quote'], u['quote']), default=None)
    return best if best is not None and ach.sim(best['quote'], u['quote']) >= cut else None


def pair_units(units_a, units_b, n, seed, ver_a, ver_b):
    """두 버전 비교용: 절반은 A 단위에서, 절반은 B 단위에서 유형별로 고르고(같은 문장은 한 번만),
    문장마다 두 버전의 답을 [A, B] 순서로 붙인다. 한쪽에서 단위로 안 나왔으면 missing."""
    picked_a = sample_units(units_a, n // 2, seed)
    picked_b = [u for u in sample_units(units_b, n, seed + 1) if _match(u, picked_a) is None][:n - len(picked_a)]
    out = []
    for u in picked_a:
        out.append({**u, 'ai': [_answer(u, ver_a), _answer(_match(u, units_b), ver_b)]})
    for u in picked_b:
        out.append({**u, 'ai': [_answer(_match(u, units_a), ver_a), _answer(u, ver_b)]})
    random.Random(seed).shuffle(out)                     # 어느 버전에서 뽑았는지 순서로 드러나지 않게
    return out


def sample_units(units, n, seed):
    """유형별로 돌아가며 하나씩(유형 안은 무작위) n개."""
    rng = random.Random(seed)
    by_type = defaultdict(list)
    for u in units:
        by_type[u['ai_type']].append(u)
    for v in by_type.values():
        rng.shuffle(v)
    picked, order = [], sorted(by_type)
    while len(picked) < n and any(by_type.values()):
        for t in order:
            if by_type[t] and len(picked) < n:
                picked.append(by_type[t].pop())
    return picked


def balanced(cases, n, seed, exclude=()):
    """판정별로 고르게 n건. 판단보류는 근거가 있었던 사건(사건 창 근거 없음 제외)만."""
    rng = random.Random(seed)
    groups = {v: [c for c in cases if c['verdict'] == v and (v != '판단보류' or c.get('reason') != 'no_evidence_in_window')
                  and c.get('selected_docs') and c['drop_id'] not in exclude] for v in VERDICTS}
    for g in groups.values():
        rng.shuffle(g)
    picked = []
    while len(picked) < n and any(groups.values()):
        for v in VERDICTS:
            if groups[v] and len(picked) < n:
                picked.append(groups[v].pop())
    return picked


def _verdict(c):
    return {'ver': c.get('prompt_version', ''), 'verdict': c['verdict'], 'hypothesis': c.get('hypothesis'),
            'reason': c.get('reason'), 'flags': {k: v for k, v in (c.get('flags') or {}).items() if v},
            'support': c.get('support'), 'refute': c.get('refute'),
            'units': [{'type': u['type'], 'direction': u['direction'], 'event': u.get('event', ''), 'w': u.get('w')}
                      for u in c.get('units', []) if u['type'] != 'X0']}


def sample_cases(cases, n, seed, other=None):
    """사례 n건. other(두 번째 버전의 {drop_id: 결과})가 있으면 두 버전의 판정을 [A, B] 순서로 붙인다."""
    return [{'drop_id': c['drop_id'], 'company': c.get('company', c['company_name']), 'D': c['D'], 'z': c.get('z'),
             'docs': [{'title': d.get('title', ''), 'url': d.get('url', ''), 'published_at': d.get('published_at', ''),
                       'S_sel': d.get('S_sel')} for d in c['selected_docs']],
             'ai': [_verdict(c)] + ([_verdict(other[c['drop_id']])] if other and c['drop_id'] in other else [])}
            for c in balanced(cases, n, seed)]


def build(ach_dir, years, n_units, n_cases, seed, names_path=None, ach2=None):
    names = load_names(names_path if names_path is not None else names_file_for(ach_dir))
    cases = load_cases(ach_dir, years, names)
    source = str(Path(ach_dir).as_posix())
    if ach2 is None:
        units = [{**u, 'ai': [_answer(u, u['ver'])]} for u in sample_units(reviewable_units(cases), n_units, seed)]
        case_rows = sample_cases(cases, n_cases, seed)
    else:                                                # 두 버전 비교: 두 번째 폴더에 있는 사건만
        other = {c['drop_id']: c for c in load_cases(ach2, years, names)}
        cases = [c for c in cases if c['drop_id'] in other]
        ua, ub = reviewable_units(cases), reviewable_units(list(other.values()))
        ver_a = cases[0].get('prompt_version', 'A') if cases else 'A'
        ver_b = next(iter(other.values())).get('prompt_version', 'B') if other else 'B'
        units = pair_units(ua, ub, n_units, seed, ver_a, ver_b)
        case_rows = sample_cases(cases, n_cases, seed, other)
        source += '+' + str(Path(ach2).as_posix())
    return {'source': source, 'years': sorted(years) if years else None, 'seed': seed,
            'types': {k: list(v) for k, v in ach.TYPES.items()}, 'units': units, 'cases': case_rows}


def write_html(data, path):
    blob = json.dumps(data, ensure_ascii=False).replace('</', '<\\/')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PAGE.replace('__DATA__', blob), encoding='utf-8')


def _set(v):
    return {x for x in (v or '').split('|') if x}


def _rate(xs):
    xs = list(xs)
    return round(sum(xs) / len(xs), 3) if xs else None


def _version_stats(units, cases, p):
    """p = '' (첫 번째 버전) 또는 '2'. 사람 답과의 일치율. 그 버전에서 단위로 안 나온 문장은 extracted에서 빠진다."""
    about = [r for r in units if r.get('about_company') == '예']
    got = [r for r in about if r.get(f'ai{p}_type')]
    blind = [r for r in got if _set(r.get('first_types'))]
    misses = Counter(f"{r[f'ai{p}_type']}→{'|'.join(sorted(_set(r['types'])))}" for r in got if r[f'ai{p}_type'] not in _set(r['types']))
    blind_dir = [r for r in got if r.get('first_direction')]
    vc = [r for r in cases if r.get(f'ai{p}_verdict')]
    return {
        'version': next((r[f'ai{p}_ver'] for r in units + cases if r.get(f'ai{p}_ver')), ''),
        'units_extracted': f'{len(got)}/{len(about)}',
        'type_agree': _rate(r[f'ai{p}_type'] in _set(r['types']) for r in got),               # AI 유형이 사람이 고른 유형 중 하나
        'type_agree_blind': _rate(r[f'ai{p}_type'] in _set(r['first_types']) for r in blind),  # 공개 전 첫 답 기준
        'direction_agree': _rate(r.get('direction') == r[f'ai{p}_direction'] for r in got),
        'direction_agree_blind': _rate(r['first_direction'] == r[f'ai{p}_direction'] for r in blind_dir),
        'type_misses_ai_to_human': [f'{k} {n}' for k, n in misses.most_common(10)],
        'verdict_agree': _rate(r['verdict'] == r[f'ai{p}_verdict'] for r in vc),
        'verdict_agree_blind': _rate(r.get('first_verdict') == r[f'ai{p}_verdict'] for r in vc if r.get('first_verdict')),
        'hypothesis_agree': _rate(r[f'ai{p}_hypothesis'] in _set(r.get('hypotheses')) for r in vc
                                  if r[f'ai{p}_verdict'] != '판단보류' and _set(r.get('hypotheses'))),
        'verdict_pairs_ai_to_human': dict(Counter(f"{r[f'ai{p}_verdict']}→{r['verdict']}" for r in vc)),
    }


def compare(rows):
    units = [r for r in rows if r['kind'] == 'unit' and _set(r.get('types'))]
    cases = [r for r in rows if r['kind'] == 'case' and r.get('verdict')]
    about = [r for r in units if r.get('about_company') == '예']
    res = {'units_reviewed': len(units),
           'not_about_company': sum(r.get('about_company') == '아니오' for r in units),
           'unsure_about_company': sum(r.get('about_company') == '모름' for r in units),
           'multi_type_share': _rate(len(_set(r['types'])) > 1 for r in about),
           'changed_after_reveal': sum(r.get('changed_after_reveal') == 'Y' for r in units + cases),
           'cases_reviewed': len(cases),
           'inconsistent_cases': sum(bool(_set(r.get('hypotheses'))) and (
               (r['verdict'] == '가치훼손' and not _set(r['hypotheses']) & {'H1', 'H2'}) or
               (r['verdict'] == '비가치' and not _set(r['hypotheses']) & {'H3', 'H4', 'H5'})) for r in cases)}
    res.update(_version_stats(units, cases, ''))
    if any(r.get('ai2_ver') for r in units + cases):
        res['version2'] = _version_stats(units, cases, '2')
    res['limits'] = ['검수자 1명', '표본은 유형·판정별로 고르게 뽑아 전체 비율과 다름(일치율을 전체 정확도로 읽지 않는다)',
                     '공개 뒤 고친 답은 AI 답을 본 영향이 있을 수 있어 _blind 값을 같이 본다',
                     '두 버전 비교의 공개 뒤 답은 두 버전 답을 모두 본 뒤라 버전 간 비교는 _blind로 한다']
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ach', type=Path, help='ACH 결과 폴더')
    ap.add_argument('--years', help='사건 연도 (쉼표). 예: 2023,2024 (튜닝용만)')
    ap.add_argument('--units', type=int, default=30)
    ap.add_argument('--cases', type=int, default=12)
    ap.add_argument('--seed', type=int, default=2026)
    ap.add_argument('--names', type=Path, help='회사명 표 (기본: 표본 폴더의 stock_names.csv)')
    ap.add_argument('--out', type=Path, help='기본: cache/review/<ach 상위 폴더 이름>/')
    ap.add_argument('--compare', type=Path, help='검수 화면에서 내려받은 CSV')
    ap.add_argument('--ach2', type=Path, help='두 번째 버전 결과 폴더(예: 지시문 4판). 이 폴더에 있는 사건만 쓴다')
    ap.add_argument('--pick-events', type=int, help='새 검수용 사건 n건을 판정별로 고르게 골라 events.txt로 쓴다')
    ap.add_argument('--exclude-review', type=Path, action='append', default=[], help='이미 검수한 결과 CSV (그 사건은 뺀다)')
    args = ap.parse_args(argv)
    if args.pick_events:
        done = {r['drop_id'] for p in args.exclude_review for r in csv.DictReader(p.open(encoding='utf-8-sig'))}
        years = set(args.years.split(',')) if args.years else None
        picked = balanced(load_cases(args.ach, years), args.pick_events, args.seed, done)
        out = args.out or OUT_ROOT / args.ach.parent.name
        out.mkdir(parents=True, exist_ok=True)
        (out / 'events.txt').write_text('\n'.join(sorted(c['drop_id'] for c in picked)) + '\n', encoding='utf-8', newline='\n')
        print(f"{out / 'events.txt'}: {len(picked)}건 (이미 검수한 {len(done)}건 제외)", dict(Counter(c['verdict'] for c in picked)))
        return 0
    if args.compare:
        rows = list(csv.DictReader(args.compare.open(encoding='utf-8-sig')))
        res = compare(rows)
        out = args.compare.with_name('ach_review_compare.json')
        out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding='utf-8')
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    if not args.ach:
        ap.error('--ach 또는 --compare가 필요하다')
    years = set(args.years.split(',')) if args.years else None
    data = build(args.ach, years, args.units, args.cases, args.seed, args.names, args.ach2)
    out = args.out or OUT_ROOT / args.ach.parent.name
    write_html(data, out / 'ach_review.html')
    print(f"검수 화면: {out / 'ach_review.html'}  (단위 {len(data['units'])}개, 사례 {len(data['cases'])}건)")
    print('유형별 단위:', dict(Counter(u['ai_type'] for u in data['units'])))
    print('판정별 사례:', dict(Counter(c['ai'][0]['verdict'] for c in data['cases'])))
    if args.ach2:
        print('단위 버전별 추출:', {i: sum(not u['ai'][i].get('missing') for u in data['units']) for i in (0, 1)})
    return 0


PAGE = r'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ACH 판정 검수</title>
<style>
:root{--bg:#EEF1F5;--card:#fff;--ink:#16202B;--muted:#5A6774;--line:#D3DAE2;--soft:#F6F8FA;
--acc:#2F4BD8;--accs:#E3E8FD;--ok:#1B7F4B;--oks:#DFF2E6;--bad:#B4400D;--bads:#FBE6DA;--warn:#8A6100;--warns:#FBF0D3}
@media (prefers-color-scheme:dark){:root{--bg:#0F151C;--card:#18212B;--ink:#E6ECF2;--muted:#9AA7B4;--line:#2B3744;--soft:#1D2733;
--acc:#93A6FF;--accs:#232D57;--ok:#5BD08E;--oks:#153626;--bad:#FB9A5C;--bads:#3A2418;--warn:#E8C060;--warns:#3A3016}}
*{box-sizing:border-box}html,body{margin:0}body{background:var(--bg);color:var(--ink);font:16px/1.65 "Malgun Gothic","Apple SD Gothic Neo",system-ui,sans-serif}
button{font:inherit;color:inherit}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line);padding:10px 20px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
header h1{font-size:18px;margin:0}
.bar{flex:1 1 200px;min-width:160px;height:8px;background:var(--line);border-radius:4px;overflow:hidden}.bar i{display:block;height:100%;background:var(--ok)}
.prog{font-size:14px;color:var(--muted);white-space:nowrap}
.hbtn{border:1px solid var(--line);background:var(--card);border-radius:6px;padding:6px 12px;cursor:pointer;font-size:14px}
.hbtn.pri{background:var(--acc);border-color:var(--acc);color:var(--card);font-weight:700}
.layout{display:grid;grid-template-columns:260px minmax(0,1fr);gap:20px;max-width:1240px;margin:0 auto;padding:20px}
aside{position:sticky;top:70px;align-self:start;max-height:calc(100vh - 90px);overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
.tabs{display:flex;border-bottom:1px solid var(--line)}.tabs button{flex:1;border:0;background:none;padding:10px;cursor:pointer;font-weight:700;color:var(--muted)}
.tabs button[aria-selected=true]{color:var(--acc);box-shadow:inset 0 -3px var(--acc)}
.list button{display:flex;width:100%;gap:8px;align-items:center;border:0;background:none;text-align:left;padding:8px 12px;cursor:pointer;font-size:14px;border-bottom:1px solid var(--soft)}
.list button:hover{background:var(--soft)}.list button[aria-current=true]{background:var(--accs)}
.dot{flex:none;width:10px;height:10px;border-radius:50%;border:2px solid var(--line)}
.dot.done{background:var(--ok);border-color:var(--ok)}.dot.diff{background:var(--bad);border-color:var(--bad)}.dot.part{border-color:var(--warn)}.dot.mix{background:var(--warn);border-color:var(--warn)}
.vline{padding:4px 0}.vline+.vline{border-top:1px dashed var(--line)}
.list .lt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.list .ln{color:var(--muted);font-size:12px;flex:none}
main{min-width:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:22px 26px;margin-bottom:16px}
.crumb{font-size:14px;color:var(--muted)}.crumb b{color:var(--ink);font-size:20px;margin-right:8px}
.quote{font-size:19px;line-height:1.75;background:var(--accs);border-left:4px solid var(--acc);padding:14px 18px;border-radius:0 8px 8px 0;margin:14px 0 8px}
.src{font-size:14px;color:var(--muted)}a{color:var(--acc)}
.q{margin-top:22px}.q h3{font-size:15px;margin:0 0 8px}.q h3 small{font-weight:400;color:var(--muted);margin-left:6px}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{border:1.5px solid var(--line);background:var(--card);border-radius:99px;padding:6px 16px;cursor:pointer;font-size:15px}
.chip[aria-pressed=true]{background:var(--acc);border-color:var(--acc);color:var(--card);font-weight:700}
.groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.grp{border:1px solid var(--line);border-radius:8px;padding:8px 10px}.grp h4{margin:0 0 4px;font-size:13px;color:var(--muted);letter-spacing:.02em}
.opt{display:flex;gap:8px;align-items:flex-start;padding:5px 6px;border-radius:6px;cursor:pointer;font-size:14.5px}
.opt:hover{background:var(--soft)}.opt input{margin-top:5px;accent-color:var(--acc);width:16px;height:16px;flex:none}
.opt.on{background:var(--accs)}.opt .ex{display:block;font-size:12.5px;color:var(--muted);line-height:1.4}
.opt.ai{outline:2px dashed var(--ok);outline-offset:-2px}
textarea{width:100%;font:inherit;font-size:15px;background:var(--soft);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:8px 10px}
.reveal{margin-top:22px;border-radius:8px;padding:14px 18px;font-size:15px}
.same{background:var(--oks)}.diffr{background:var(--bads)}.pending{background:var(--soft);border:1px dashed var(--line)}
.reveal b.tag{display:inline-block;padding:1px 10px;border-radius:99px;background:var(--card);margin-right:6px}
.foot{display:flex;justify-content:space-between;gap:10px;margin-top:20px;flex-wrap:wrap}
.btn{border:0;background:var(--acc);color:var(--card);padding:9px 18px;border-radius:7px;cursor:pointer;font-weight:700}
.btn.sec{background:transparent;color:var(--acc);border:1.5px solid var(--acc)}.btn[disabled]{opacity:.4;cursor:default}
table{border-collapse:collapse;width:100%;font-size:14.5px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:400;font-size:13px}
.hint{font-size:13px;color:var(--muted)}.note{background:var(--warns);color:var(--warn);border-radius:6px;padding:8px 12px;font-size:14px;margin-top:10px}
.help{display:none}.help.on{display:block}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--card);padding:8px 16px;border-radius:6px;font-size:14px;z-index:9}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
@media (max-width:820px){.layout{grid-template-columns:1fr}aside{position:static;max-height:260px}}
</style></head><body>
<header><h1>ACH 판정 검수</h1><div class="bar" aria-hidden="true"><i id="pbar"></i></div><span class="prog" id="prog"></span>
<button class="hbtn" id="b-help">검수 방법</button><button class="hbtn" id="b-load">불러오기</button><input type="file" id="f-load" accept=".csv" hidden>
<button class="hbtn pri" id="b-save">결과 CSV 내려받기</button></header>
<div class="layout">
<aside><div class="tabs" role="tablist"><button role="tab" id="tab-unit">단위</button><button role="tab" id="tab-case">사례</button></div><div class="list" id="list"></div></aside>
<main>
<div class="card help" id="help"><h3 style="margin-top:0">검수 방법</h3>
<p><b>단위</b>: 인용 문장과 원문을 보고 ① 대상 회사 이야기인지 ② 유형(해당하는 것 모두) ③ 방향을 고른다. 그다음 <b>AI 답 보기</b>를 누른다.</p>
<p><b>사례</b>: 선정 기사 제목을 보고(필요하면 원문을 열어) 판정과 가설(해당하는 것 모두)을 고른 뒤 <b>모델 판정 보기</b>를 누른다.</p>
<p>AI 답이 두 줄이면 분류 지시문 두 판(3판·4판)의 답이다. 어느 판이 더 맞는지 보려는 검수라, 공개 전에 직접 고른 답이 가장 중요하다.</p>
<p>공개 뒤에도 답을 고칠 수 있다. 공개 전 첫 답은 따로 기록돼, AI 답을 보기 전의 판단을 잃지 않는다.
답은 이 브라우저에 자동 저장된다. 다른 컴퓨터·브라우저로 옮길 때는 CSV를 내려받아 <b>불러오기</b>로 다시 읽는다.</p>
<p class="hint">단축키: ← → 이전·다음 (글 입력 중에는 꺼짐)</p></div>
<div id="panel"></div></main></div>
<script>
const D=__DATA__;
const KEY='ach_review:'+D.source+':'+D.seed;
const FAM=[['F','회사 펀더멘털'],['C','자본·재무·지배구조'],['M','시장·업종'],['S','수급'],['R','의견·해명'],['X','정보 없음']];
const HYP=[['H1','실적 훼손','가치훼손'],['H2','희석','가치훼손'],['H3','심리','비가치'],['H4','수급','비가치'],['H5','소문','비가치']];
const verName=v=>({'ach-units-3':'지시문 3판','ach-units-4':'지시문 4판'}[v]||v||'AI');
let S={unit:{},case:{},mode:'unit',iu:0,ic:0};
try{const s=JSON.parse(localStorage.getItem(KEY)||'null');if(s)S=Object.assign(S,s)}catch(e){}
// 이전 화면 형식(유형 하나, 가설 문자열)을 새 형식으로
Object.values(S.unit).forEach(a=>{if(a.type&&!a.types){a.types=[a.type];delete a.type}});
Object.values(S.case).forEach(a=>{if(typeof a.hypothesis==='string'){a.hypotheses=a.hypothesis==='없음'?[]:[a.hypothesis.slice(0,2)];delete a.hypothesis}});
const save=()=>{try{localStorage.setItem(KEY,JSON.stringify(S))}catch(e){}};
const $=s=>document.querySelector(s);
const esc=t=>String(t??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const same=(a,b)=>JSON.stringify([...(a||[])].sort())===JSON.stringify([...(b||[])].sort());
function toast(t){const n=document.createElement('div');n.className='toast';n.textContent=t;document.body.appendChild(n);setTimeout(()=>n.remove(),2200)}
function rec(kind,i){return S[kind][i]=S[kind][i]||{}}
function unitDone(a){return a&&a.about_company&&a.types&&a.types.length&&a.direction}
function caseDone(a){return a&&a.verdict&&a.hypotheses}
function ansMatch(x,a){return !x.missing&&(a.types||[]).includes(x.type)&&a.direction===x.direction}
function markChange(a){if(a.revealed)a.changed_after_reveal=true}
function grade(m,n){return n&&m===n?'done':m?'mix':'diff'}
function status(kind,i){
  const a=S[kind][i];if(!a)return '';
  if(kind==='unit'){if(!a.revealed)return unitDone(a)?'part':'';const got=D.units[i].ai.filter(x=>!x.missing);return grade(got.filter(x=>ansMatch(x,a)).length,got.length)}
  if(!a.revealed)return caseDone(a)?'part':'';const v=D.cases[i].ai;return grade(v.filter(x=>x.verdict===a.verdict).length,v.length);
}
function renderList(){
  const kind=S.mode,items=kind==='unit'?D.units:D.cases,cur=kind==='unit'?S.iu:S.ic;
  $('#tab-unit').setAttribute('aria-selected',kind==='unit');$('#tab-case').setAttribute('aria-selected',kind==='case');
  $('#list').innerHTML=items.map((x,i)=>'<button data-i="'+i+'" aria-current="'+(i===cur)+'"><span class="dot '+status(kind,i)+'"></span><span class="lt">'+
    (i+1)+'. '+esc(x.company)+'</span><span class="ln">'+esc(x.D)+'</span></button>').join('');
  $('#list').querySelectorAll('button').forEach(b=>b.onclick=()=>{if(kind==='unit')S.iu=+b.dataset.i;else S.ic=+b.dataset.i;save();render()});
  const du=Object.values(S.unit).filter(a=>a.revealed).length,dc=Object.values(S.case).filter(a=>a.revealed).length,tot=D.units.length+D.cases.length;
  $('#prog').textContent='단위 '+du+'/'+D.units.length+' · 사례 '+dc+'/'+D.cases.length;$('#pbar').style.width=((du+dc)/tot*100)+'%';
}
function chips(name,opts,val){return '<div class="chips" data-name="'+name+'">'+opts.map(o=>'<button type="button" class="chip" aria-pressed="'+(val===o)+'" data-v="'+esc(o)+'">'+esc(o)+'</button>').join('')+'</div>'}
function typeGrid(sel,ai){
  return '<div class="groups">'+FAM.map(([f,fn])=>'<div class="grp"><h4>'+f+' · '+fn+'</h4>'+Object.entries(D.types).filter(([k])=>k[0]===f).map(([k,[n,ex]])=>
    '<label class="opt'+(sel.includes(k)?' on':'')+(ai&&ai.includes(k)?' ai':'')+'"><input type="checkbox" data-t="'+k+'"'+(sel.includes(k)?' checked':'')+'><span><b>'+k+'</b> '+esc(n)+'<span class="ex">'+esc(ex)+'</span></span></label>').join('')+'</div>').join('')+'</div>';
}
function foot(kind,i,n,label){
  const a=S[kind][i]||{};
  return (a.revealed?'':'<div class="reveal pending">답을 고른 뒤 <button class="btn" id="rv">'+label+'</button></div>')+
   '<div class="foot"><button class="btn sec" id="pv"'+(i===0?' disabled':'')+'>← 이전</button><button class="btn sec" id="nx"'+(i===n-1?' disabled':'')+'>다음 →</button></div>';
}
function bindNav(kind,n){
  const go=d=>{if(kind==='unit')S.iu=Math.max(0,Math.min(n-1,S.iu+d));else S.ic=Math.max(0,Math.min(n-1,S.ic+d));save();render();window.scrollTo(0,0)};
  $('#pv').onclick=()=>go(-1);$('#nx').onclick=()=>go(1);
}
function unitView(){
  const i=S.iu,u=D.units[i],a=S.unit[i]||{},sel=a.types||[];
  let h='<div class="card"><div class="crumb"><b>'+esc(u.company)+'</b>급락일 '+esc(u.D)+' · 단위 '+(i+1)+' / '+D.units.length+'</div>'+
   '<div class="quote">'+esc(u.quote)+'</div><div class="src">기사: '+esc(u.title||'(제목 없음)')+' · <a href="'+esc(u.url)+'" target="_blank" rel="noopener">원문 열기 ↗</a></div>'+
   '<div class="q"><h3>① 대상 회사('+esc(u.company)+')에 관한 문장인가?</h3>'+chips('about',['예','아니오','모름'],a.about_company)+'</div>'+
   '<div class="q"><h3>② 유형 <small>해당하는 것을 모두 고른다</small></h3>'+typeGrid(sel,a.revealed?u.ai.filter(x=>!x.missing).map(x=>x.type):null)+'</div>'+
   '<div class="q"><h3>③ 방향 <small>대상 회사에 나쁜 소식이면 부정</small></h3>'+chips('dir',['부정','긍정','없음'],a.direction)+'</div>'+
   '<div class="q"><h3>메모</h3><textarea id="nt" rows="2" placeholder="애매했던 점, 다른 유형이라고 본 이유 등">'+esc(a.note||'')+'</textarea></div>';
  if(a.revealed){const st=status('unit',i);
    h+='<div class="reveal '+(st==='done'?'same':st==='diff'?'diffr':'pending')+'">'+u.ai.map(x=>'<div class="vline"><b class="tag">'+esc(verName(x.ver))+'</b> '+
      (x.missing?'<span class="hint">이 문장을 단위로 내지 않았다</span>':'<b>'+x.type+' '+esc(D.types[x.type]?D.types[x.type][0]:'')+'</b> / <b>'+x.direction+'</b> — '+
       (ansMatch(x,a)?'당신의 답과 맞음':'당신의 답과 다름')+' <span class="hint">(3번 분류: '+esc((x.run_types||[]).join(', '))+')</span>')+'</div>').join('')+
      '<span class="hint">사건 이름: '+esc(u.event)+' · 유형표에서 AI 답은 초록 점선</span>'+
      (a.first_types&&!same(a.first_types,sel)||a.first_direction&&a.first_direction!==a.direction?'<div class="note">공개 전 첫 답: '+esc((a.first_types||[]).join(', '))+' / '+esc(a.first_direction||'')+' (수정됨)</div>':'')+'</div>';}
  h+=foot('unit',i,D.units.length,'AI 답 보기')+'</div>';
  $('#panel').innerHTML=h;
  $('#panel').querySelectorAll('.chips').forEach(c=>c.querySelectorAll('.chip').forEach(b=>b.onclick=()=>{const r=rec('unit',i);markChange(r);r[c.dataset.name==='about'?'about_company':'direction']=b.dataset.v;save();render()}));
  $('#panel').querySelectorAll('input[data-t]').forEach(cb=>cb.onchange=()=>{const r=rec('unit',i);markChange(r);r.types=r.types||[];const t=cb.dataset.t;
    r.types=cb.checked?[...new Set([...r.types,t])]:r.types.filter(x=>x!==t);save();render()});
  $('#nt').oninput=e=>{rec('unit',i).note=e.target.value;save()};
  if($('#rv'))$('#rv').onclick=()=>{const r=rec('unit',i);if(!unitDone(r)){toast('①②③을 모두 고른 뒤 공개한다');return}
    r.first_types=[...r.types];r.first_direction=r.direction;r.first_about=r.about_company;r.revealed=true;r.changed_after_reveal=false;save();render()};
  bindNav('unit',D.units.length);
}
function caseView(){
  const i=S.ic,c=D.cases[i],a=S.case[i]||{},hy=a.hypotheses||[];
  let h='<div class="card"><div class="crumb"><b>'+esc(c.company)+'</b>급락일 '+esc(c.D)+' · z '+esc(c.z)+' · 사례 '+(i+1)+' / '+D.cases.length+'</div>'+
   '<div class="q"><h3>선정 기사 <small>판정에 쓴 기사들. 제목을 클릭하면 원문이 열린다</small></h3><table><tr><th style="width:130px">입력 시각</th><th>제목</th><th style="width:60px">점수</th></tr>'+
   c.docs.map(d=>'<tr><td>'+esc((d.published_at||'').slice(0,16).replace('T',' '))+'</td><td><a href="'+esc(d.url)+'" target="_blank" rel="noopener">'+esc(d.title)+'</a></td><td>'+esc(d.S_sel)+'</td></tr>').join('')+'</table></div>'+
   '<div class="q"><h3>① 당신의 판정</h3>'+chips('verdict',['가치훼손','비가치','판단보류'],a.verdict)+'</div>'+
   '<div class="q"><h3>② 급락의 실제 원인으로 보는 가설 <small>해당하는 것을 모두. 판단보류면 비워도 된다</small></h3>'+
   '<p class="hint" style="margin:0 0 8px">H1·H2를 고르면 가치훼손 쪽, H3~H5를 고르면 비가치 쪽이다. 판정과 같은 쪽 가설을 고른다.</p><div class="chips">'+
    HYP.map(([k,n,g])=>'<button type="button" class="chip hy" aria-pressed="'+hy.includes(k)+'" data-v="'+k+'">'+k+' '+n+' <small>· '+g+'</small></button>').join('')+'</div>'+
   (a.verdict&&hy.length&&((a.verdict==='가치훼손'&&!hy.some(k=>k==='H1'||k==='H2'))||(a.verdict==='비가치'&&!hy.some(k=>['H3','H4','H5'].includes(k))))?
    '<div class="note">판정('+esc(a.verdict)+')과 가설의 방향이 맞지 않는다. 판정을 고치거나, 급락의 실제 원인으로 보는 가설만 남긴다.</div>':'')+'</div>'+
   '<div class="q"><h3>판단 근거 메모</h3><textarea id="nt" rows="3" placeholder="어느 기사가 결정적이었는지, 모델이 놓친 것 등">'+esc(a.note||'')+'</textarea></div>';
  if(a.revealed){const st=status('case',i),H=['H1','H2','H3','H4','H5'];
    h+='<div class="reveal '+(st==='done'?'same':st==='diff'?'diffr':'pending')+'">'+c.ai.map(x=>{const m=a.verdict===x.verdict;
      return '<div class="vline"><b class="tag">'+esc(verName(x.ver))+'</b> <b>'+x.verdict+'</b> ('+esc(x.hypothesis||'')+(x.reason?', 이유: '+esc(x.reason):'')+') — '+(m?'당신의 판정과 맞음':'당신의 판정과 다름')+
      (Object.keys(x.flags||{}).length?'<br><span class="hint">표시: '+esc(Object.keys(x.flags).join(', '))+'</span>':'')+
      (x.support?'<table style="margin-top:8px"><tr><th></th>'+H.map(k=>'<th>'+k+'</th>').join('')+'</tr><tr><td>지지</td>'+H.map(k=>'<td>'+esc((x.support[k]||0).toFixed(2))+'</td>').join('')+'</tr><tr><td>반박</td>'+H.map(k=>'<td>'+esc((x.refute[k]||0).toFixed(2))+'</td>').join('')+'</tr></table>':'')+
      '<details style="margin-top:6px"><summary>쓴 단위 '+x.units.length+'개</summary><table><tr><th>유형</th><th>방향</th><th>무게</th><th>사건</th></tr>'+
      x.units.map(u=>'<tr><td>'+u.type+'</td><td>'+u.direction+'</td><td>'+esc(u.w)+'</td><td>'+esc(u.event)+'</td></tr>').join('')+'</table></details></div>'}).join('')+
      (a.first_verdict&&a.first_verdict!==a.verdict?'<div class="note">공개 전 첫 판정: '+esc(a.first_verdict)+' (수정됨)</div>':'')+'</div>';}
  h+=foot('case',i,D.cases.length,'모델 판정 보기')+'</div>';
  $('#panel').innerHTML=h;
  $('#panel').querySelectorAll('.chips[data-name=verdict] .chip').forEach(b=>b.onclick=()=>{const r=rec('case',i);markChange(r);r.verdict=b.dataset.v;r.hypotheses=r.hypotheses||[];save();render()});
  $('#panel').querySelectorAll('.chip.hy').forEach(b=>b.onclick=()=>{const r=rec('case',i);markChange(r);r.hypotheses=r.hypotheses||[];const k=b.dataset.v;
    r.hypotheses=r.hypotheses.includes(k)?r.hypotheses.filter(x=>x!==k):[...r.hypotheses,k];save();render()});
  $('#nt').oninput=e=>{rec('case',i).note=e.target.value;save()};
  if($('#rv'))$('#rv').onclick=()=>{const r=rec('case',i);r.hypotheses=r.hypotheses||[];if(!r.verdict){toast('판정을 고른 뒤 공개한다');return}
    r.first_verdict=r.verdict;r.first_hypotheses=[...r.hypotheses];r.revealed=true;r.changed_after_reveal=false;save();render()};
  bindNav('case',D.cases.length);
}
function render(){renderList();S.mode==='unit'?unitView():caseView()}
$('#tab-unit').onclick=()=>{S.mode='unit';save();render()};$('#tab-case').onclick=()=>{S.mode='case';save();render()};
$('#b-help').onclick=()=>$('#help').classList.toggle('on');
document.addEventListener('keydown',e=>{if(/INPUT|TEXTAREA|SELECT/.test(e.target.tagName))return;if(e.key==='ArrowRight'&&$('#nx')&&!$('#nx').disabled)$('#nx').click();if(e.key==='ArrowLeft'&&$('#pv')&&!$('#pv').disabled)$('#pv').click()});
const COLS=['kind','idx','drop_id','unit_id','company','about_company','types','direction','verdict','hypotheses','note','first_types','first_direction','first_verdict','first_hypotheses','revealed','changed_after_reveal',
  'ai_ver','ai_type','ai_direction','ai_verdict','ai_hypothesis','ai2_ver','ai2_type','ai2_direction','ai2_verdict','ai2_hypothesis'];
$('#b-save').onclick=()=>{
  const q=v=>'"'+String(v??'').replace(/"/g,'""')+'"',j=v=>(v||[]).join('|'),rows=[COLS.join(',')];
  const ua=(u,k)=>{const x=u.ai[k];return x?[x.ver,x.missing?'':x.type,x.missing?'':x.direction,'','']:['','','','','']};
  const ca=(c,k)=>{const x=c.ai[k];return x?[x.ver,'','',x.verdict,x.hypothesis]:['','','','','']};
  D.units.forEach((u,i)=>{const a=S.unit[i];if(!a)return;rows.push(['unit',i,u.drop_id,u.unit_id,u.company,a.about_company,j(a.types),a.direction,'','',a.note,j(a.first_types),a.first_direction,'','',a.revealed?'Y':'',a.changed_after_reveal?'Y':'',...ua(u,0),...ua(u,1)].map(q).join(','))});
  D.cases.forEach((c,i)=>{const a=S.case[i];if(!a)return;rows.push(['case',i,c.drop_id,'',c.company,'','','',a.verdict,j(a.hypotheses),a.note,'','',a.first_verdict,j(a.first_hypotheses),a.revealed?'Y':'',a.changed_after_reveal?'Y':'',...ca(c,0),...ca(c,1)].map(q).join(','))});
  const blob=new Blob(['﻿'+rows.join('\r\n')],{type:'text/csv'});const l=document.createElement('a');l.href=URL.createObjectURL(blob);l.download='ach_review_result.csv';document.body.appendChild(l);l.click();l.remove();toast('내려받았다: ach_review_result.csv');
};
function parseCSV(t){const rows=[];let r=[],f='',q=false;for(let i=0;i<t.length;i++){const c=t[i];
  if(q){if(c==='"'){if(t[i+1]==='"'){f+='"';i++}else q=false}else f+=c}
  else if(c==='"')q=true;else if(c===','){r.push(f);f=''}else if(c==='\n'){r.push(f);rows.push(r);r=[];f=''}else if(c!=='\r')f+=c}
  if(f||r.length){r.push(f);rows.push(r)}return rows}
$('#b-load').onclick=()=>$('#f-load').click();
$('#f-load').onchange=e=>{const file=e.target.files[0];if(!file)return;file.text().then(t=>{
  const rows=parseCSV(t.replace(/^﻿/,''));const h=rows.shift();let n=0;const sp=v=>v?v.split('|').filter(Boolean):[];
  rows.forEach(r=>{const o={};h.forEach((k,i)=>o[k]=r[i]);const i=+o.idx;
    if(o.kind==='unit'&&D.units[i]&&D.units[i].drop_id===o.drop_id){S.unit[i]={about_company:o.about_company,types:sp(o.types),direction:o.direction,note:o.note,first_types:sp(o.first_types),first_direction:o.first_direction,revealed:o.revealed==='Y',changed_after_reveal:o.changed_after_reveal==='Y'};n++}
    if(o.kind==='case'&&D.cases[i]&&D.cases[i].drop_id===o.drop_id){S.case[i]={verdict:o.verdict,hypotheses:sp(o.hypotheses),note:o.note,first_verdict:o.first_verdict,first_hypotheses:sp(o.first_hypotheses),revealed:o.revealed==='Y',changed_after_reveal:o.changed_after_reveal==='Y'};n++}});
  save();render();toast(n+'개 답을 불러왔다');e.target.value=''})};
render();
</script></body></html>'''


if __name__ == '__main__':
    raise SystemExit(main())
