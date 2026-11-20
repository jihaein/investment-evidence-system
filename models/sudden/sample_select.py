"""2023~2025 백테스트 표본 고르기 (설계서 v3.1 2·7·8절).

events.py --v31이 만든 전체 사건(시가총액 1,000억 이상, 보통주, 관리종목 제외) 중 연도별로 무작위 추출한다.
2023~2024년은 튜닝용(split=tune), 2025년은 검증용(split=validate)이다. 검증용은 설정을 확정한 뒤 한 번만 돌린다.

    $env:SUDDEN_OUTPUT='output_2023'; python models/sudden/sample_select.py --per-year 67 --seed 2026

전체 사건은 drop_events_all.csv / drop_sessions_all.csv로 보관하고, 고른 사건만 drop_events.csv / drop_sessions.csv에 쓴다.
"""
import argparse
import os
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = HERE / os.getenv('SUDDEN_OUTPUT', 'output')
SPLITS = {'2023': 'tune', '2024': 'tune', '2025': 'validate',
          '2026': 'validate2026'}   # 규칙 동결 뒤 새 기간 검증 (output_2026/validation_plan.json, 2026-11-11 사전 등록)


def select(events, per_year, seed):
    ok = events[events['mktcap_ok'].astype(str).str.lower().eq('true')
                & events['D'].str[:4].isin(SPLITS) & events['news_window_start'].notna()].copy()
    picked = []
    for year, g in ok.groupby(ok['D'].str[:4]):
        picked.append(g.sample(n=min(per_year, len(g)), random_state=seed + int(year)))
    out = pd.concat(picked).sort_values(['D', 'stock_code']).reset_index(drop=True)
    out['split'] = out['D'].str[:4].map(SPLITS)
    return out


def select_codes(events, codes, start, end, split='team120'):
    """팀 백테스트 종목(codes)의 사건 전부(무작위 추출 없음). 시가총액 1,000억 이상·뉴스 창이 있는 사건만."""
    ok = events[events['mktcap_ok'].astype(str).str.lower().eq('true') & events['stock_code'].isin(codes)
                & events['D'].between(start, end) & events['news_window_start'].notna()].copy()
    out = ok.sort_values(['D', 'stock_code']).reset_index(drop=True)
    out['split'] = split
    return out


def fix_names(names, official):
    """KRX 약칭이 DART 공식 이름의 앞부분이면(잘린 이름, 예: 현대엘리베이 → 현대엘리베이터) 공식 이름으로 바꾼다.
    공식 이름과 다른 진짜 옛 이름(사명 변경)은 그대로 둔다."""
    out = names.copy()
    for i, r in out.iterrows():
        full = official.get(r['stock_code'])
        # 6자 이상일 때만: "한국전력 → 한국전력공사"처럼 기사에서 쓰는 짧은 이름을 늘리면 오히려 놓친다
        if full and full != r['company_name'] and full.startswith(r['company_name']) and len(r['company_name']) >= 6:
            out.at[i, 'company_name'] = full
    return out.drop_duplicates(['stock_code', 'company_name', 'from', 'to']).reset_index(drop=True)


def rename_events(events, names):
    """사건표의 회사명(KRX 약칭)을 보정 이름표로 바꾼다. 판정(ach)·공시(dart)는 사건표의 이름을 대상 회사로 쓴다."""
    out = events.copy()
    by_code = {code: g for code, g in names.groupby('stock_code')}
    for i, r in out.iterrows():
        g = by_code.get(r['stock_code'])
        if g is None:
            continue
        hit = g[(g['from'] <= r['D']) & (r['D'] <= g['to'])]
        if len(hit) and hit.iloc[0]['company_name'] != r['company_name']:
            out.at[i, 'company_name'] = hit.iloc[0]['company_name']
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--per-year', type=int, default=67)
    ap.add_argument('--seed', type=int, default=2026)
    ap.add_argument('--fix-names', action='store_true', help='잘린 KRX 약칭을 DART 공식 이름으로 바꾼다(원본은 stock_names_krx.csv)')
    ap.add_argument('--codes', type=Path, help='종목코드 목록(한 줄에 하나). 주면 무작위 추출 대신 이 종목의 사건을 모두 고른다')
    ap.add_argument('--start', default='2020-01-01')
    ap.add_argument('--end', default='2026-12-31')
    args = ap.parse_args(argv)
    if args.fix_names:
        import dart
        official = dart.corp_names(dart.api_key())
        krx_path, path = OUT / 'stock_names_krx.csv', OUT / 'stock_names.csv'
        if not krx_path.exists():
            path.rename(krx_path)
        names = pd.read_csv(krx_path, dtype={'stock_code': str}, encoding='utf-8-sig')
        fixed = fix_names(names, official)
        fixed.to_csv(path, index=False, encoding='utf-8-sig')
        changed = names.merge(fixed, on=['stock_code', 'from', 'to'], suffixes=('_krx', ''))
        changed = changed[changed['company_name_krx'] != changed['company_name']]
        print(f'이름 보정 {len(changed)}줄:', dict(zip(changed['company_name_krx'], changed['company_name'])) if len(changed) < 40 else '')
        for name in ('drop_events.csv', 'drop_events_all.csv'):          # 사건표의 회사명도 같은 이름으로
            p = OUT / name
            if p.exists():
                ev = pd.read_csv(p, dtype=str, keep_default_na=False, encoding='utf-8-sig')
                new = rename_events(ev, fixed)
                n = int((new['company_name'] != ev['company_name']).sum())
                if n:
                    new.to_csv(p, index=False, encoding='utf-8-sig')
                print(f'{name}: 회사명 {n}건 보정')
        return 0
    all_events, all_sessions = OUT / 'drop_events_all.csv', OUT / 'drop_sessions_all.csv'
    if not all_events.exists():                              # 처음 한 번: events.py 결과를 전체 표로 보관
        (OUT / 'drop_events.csv').rename(all_events)
        (OUT / 'drop_sessions.csv').rename(all_sessions)
    events = pd.read_csv(all_events, dtype={'stock_code': str}, encoding='utf-8-sig')
    sessions = pd.read_csv(all_sessions, encoding='utf-8-sig')
    if args.codes:
        codes = {c.strip().zfill(6) for c in args.codes.read_text(encoding='utf-8').split() if c.strip()}
        picked = select_codes(events, codes, args.start, args.end)
        print(f'지정 종목 {len(codes)}개 중 사건이 있는 종목 {picked["stock_code"].nunique()}개')
    else:
        picked = select(events, args.per_year, args.seed)
    picked.to_csv(OUT / 'drop_events.csv', index=False, encoding='utf-8-sig')
    sessions[sessions['drop_id'].isin(picked['drop_id'])].to_csv(OUT / 'drop_sessions.csv', index=False, encoding='utf-8-sig')
    print(f"전체 사건 {len(events)}건 → 시가총액 1,000억 이상 {int(events['mktcap_ok'].astype(str).str.lower().eq('true').sum())}건 "
          f"→ 표본 {len(picked)}건 (종목 {picked['stock_code'].nunique()}개)")
    print('연도·용도별:', picked.groupby([picked['D'].str[:4], 'split']).size().to_dict())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
