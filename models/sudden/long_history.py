"""long_history.py — 급락 다음날 매도 규칙을 2000~2026 전체 시장 시세로만 다시 시험한다 (뉴스·LLM 판정 없음).

[왜] 파이널 시험(120종목 2020~2026)의 성과는 대부분 '급락 뒤에 판다'는 것 자체에서 나왔다(significance.py).
     이 부분은 시세만으로 계산되므로 닷컴 붕괴·금융위기까지 넣고, 나중에 상장폐지된 종목까지 넣어 생존 편향 없이 본다.
     가치훼손/비가치 분류는 여기서 검증하지 않는다(옛 기사 부족 + LLM이 이후 역사를 알고 있어 미래 정보가 샌다).

[입력] cache/krx/daily/<날짜>.csv  (krx_prices.py가 받은 그날 상장 전종목 시세. 상장폐지 종목은 정리매매까지 들어 있다)
       KOSPI·KOSDAQ 지수는 pykrx로 받아 cache/long_history/index.csv에 둔다

[급락] events.py 설계서 v3.1 그대로: 시장모형 AR(D-120~D-1 회귀), z <= -1.96, 보통주만, 관리종목 제외,
       권리락·거래정지 직후 제외, 10거래일 안 재급락은 같은 사건. 시가총액 기준은 두 가지로 나란히 본다.
         abs : D-1 시가총액 >= 1,000억 (동결 기준 그대로)
         pct : D-1 시가총액이 그날 같은 시장 보통주 중 상위 q (q = 2026년에 1,000억이 차지하는 비율). 옛 시장이 작았던 것 보정

[A] 사건 연구: 매도일 = 급락일 다음 거래일. 그 종가 이후 H 거래일 종목 누적수익률 - 같은 시장 보통주 동일가중 누적수익률.
    상장폐지로 H일 전에 끝나면 마지막 가격에서 현금으로 둔다(빼면 생존 편향). 기준선 = 같은 조건 종목의 아무 날.
    종목 단위 클러스터 부트스트랩. 국면별로 나눈다.
[B] 포트폴리오: 무작위 진입일 × 그날 조건을 만족하는 보통주 무작위 20종목, 동일 금액 매수 후 보유(리밸런싱 없음).
      보유      : 끝까지 (상장폐지되면 마지막 가격에서 현금)
      급락매도  : 종목별 첫 급락의 다음 거래일 종가에 팔고 현금(이자 0)
      무작위매도: 같은 종목 수를 아무 날에 판다 (3회 평균)
    매도 비용 0.1%(팀 엔진 COST_RATE). 누적수익률·MDD를 진입 국면별로 본다.

    .venv\\Scripts\\python.exe models\\sudden\\long_history.py
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import events as ev

HERE = Path(__file__).resolve().parent
DAILY = HERE / 'cache/krx/daily'
OUT = HERE / 'cache/long_history'
HORIZONS = (20, 60, 120, 250)
COST = 0.001
N_BOOT = 5_000
# 이름 붙인 국면 (급락일 또는 진입일 기준). 겹치지 않게 잡는다
REGIMES = [('2000-02 닷컴붕괴', '2000-03-01', '2002-12-31'),
           ('2007-09 금융위기', '2007-11-01', '2009-03-31'),
           ('2011 유럽재정위기', '2011-08-01', '2011-12-31'),
           ('2018 하락장', '2018-02-01', '2019-01-31'),
           ('2020 코로나', '2020-02-01', '2020-04-30'),
           ('2021-22 긴축하락장', '2021-07-01', '2022-12-31')]


def regime_of(dates):
    out = pd.Series('그 외', index=dates.index)
    for name, a, b in REGIMES:
        out[(dates >= a) & (dates <= b)] = name
    return out


# ---- 1. 시세 읽기 ----
def load_daily(start=None, end=None, cache=None):
    cache = cache or OUT / f"prices_{(start or '2000-01-01').replace('-', '')}.pkl"
    if cache.exists():
        p = pd.read_pickle(cache)
    else:
        frames = []
        for f in sorted(DAILY.glob('*.csv')):
            if start and f.stem < start.replace('-', ''):
                continue
            d = pd.read_csv(f, dtype=str, encoding='utf-8-sig',
                            usecols=['BAS_DD', 'ISU_CD', 'ISU_NM', 'MKT_NM', 'SECT_TP_NM', 'TDD_CLSPRC', 'FLUC_RT',
                                     'ACC_TRDVOL', 'MKTCAP'])
            if not len(d):
                continue
            frames.append(pd.DataFrame({
                'stock_code': d['ISU_CD'].str.strip(), 'company_name': d['ISU_NM'].str.strip(),
                'market': d['MKT_NM'].map(ev.market_of), 'trade_date': pd.to_datetime(d['BAS_DD'], format='%Y%m%d'),
                'close': ev.T.to_number(d['TDD_CLSPRC']), 'ret': ev.T.to_number(d['FLUC_RT']) / 100,
                'volume': ev.T.to_number(d['ACC_TRDVOL']), 'mktcap': ev.T.to_number(d['MKTCAP']),
                'sect': d['SECT_TP_NM'].fillna('').str.strip()}))
        p = pd.concat(frames, ignore_index=True)
        p = p[p['market'].isin(['KOSPI', 'KOSDAQ'])]
        for c in ('close', 'ret', 'volume', 'mktcap'):
            p[c] = p[c].astype('float32')
        p['market'] = p['market'].astype('category')
        p = p.sort_values(['stock_code', 'trade_date']).reset_index(drop=True)
        OUT.mkdir(parents=True, exist_ok=True)
        p.to_pickle(cache)
    if start:
        p = p[p['trade_date'] >= start]
    if end:
        p = p[p['trade_date'] <= end]
    return p


def load_index(prices, path=OUT / 'index.csv', refresh=False):
    if refresh or not path.exists():
        idx = ev.fetch_index_pykrx(['KOSDAQ', 'KOSPI'], prices['trade_date'].min() - pd.Timedelta(days=14),
                                   prices['trade_date'].max())
        OUT.mkdir(parents=True, exist_ok=True)
        idx.to_csv(path, index=False, encoding='utf-8-sig')
    return ev.load_index(prices, index_csv=path)


def common_mask(p):
    """events.exclusion_reason(common_only)와 같은 보통주 규칙 (벡터로)."""
    code, name = p['stock_code'].astype(str), p['company_name'].astype(str)
    return (code.str.endswith('0') & ~name.str.contains('스팩|리츠') & ~name.str.endswith(('우', '우B', '우C'))
            & ~p['sect'].astype(str).str.contains('관리'))


def size_cut(p, as_of='2026-01-01'):
    """2026년 이후 보통주 중 시가총액 1,000억 이상 비율 (시장별). pct 기준의 q."""
    late = p[(p['trade_date'] >= as_of) & p['common']]
    return late.groupby('market', observed=True)['mktcap'].apply(lambda s: float((s >= ev.MIN_MKTCAP).mean())).to_dict()


# ---- 2. 급락 사건 ----
def drop_events(p, index, q):
    """events.detect_events(v3.1)와 같은 계산을 전체 시장에 한다. abs·pct 두 시가총액 기준을 표시한다."""
    ab = ev.add_abnormal(p, index, model='market_model')
    ab['common'] = common_mask(ab)
    # 그날 같은 시장 보통주 시가총액 순위 (D-1 기준이라 하루 민다)
    rank = ab[ab['common']].groupby(['trade_date', 'market'], observed=True)['mktcap'].rank(pct=True, ascending=False)
    ab['mktcap_rank'] = rank
    ab['mktcap_rank_prev'] = ab.groupby('stock_code')['mktcap_rank'].shift(1)
    hit = ab[(ab['z'] <= ev.Z_CUT) & ab['common'] & ~ab['corp_action'] & ~ab['after_halt']].copy()
    hit['abs_ok'] = hit['mktcap_prev'] >= ev.MIN_MKTCAP
    hit['pct_ok'] = hit['mktcap_rank_prev'] <= hit['market'].map(q).astype(float)
    rows = []
    for rule in ('abs', 'pct'):
        h = hit[hit[f'{rule}_ok']].sort_values(['stock_code', 'row'])
        # 같은 사건 묶기: 첫 급락일에서 10거래일 안. 연쇄를 따라가야 하므로 종목별로 순서대로 본다
        keep = []
        for code, g in h.groupby('stock_code', sort=False):
            start = None
            for i, r in zip(g.index, g['row'].to_numpy()):
                if start is not None and r - start <= ev.GROUP_DAYS:
                    continue
                start = r
                keep.append(i)
        e = h.loc[keep, ['stock_code', 'company_name', 'market', 'trade_date', 'ret', 'ar', 'z', 'mktcap_prev']].copy()
        e['rule'] = rule
        rows.append(e)
    e = pd.concat(rows, ignore_index=True).rename(columns={'trade_date': 'drop_date'})
    return e, ab


# ---- 3. 사건 연구 ----
def growth_grids(ab):
    """날짜 × 종목 일간 수익률과, 시장별 보통주 동일가중 수익률."""
    r = ab.pivot_table(index='trade_date', columns='stock_code', values='ret', observed=True).sort_index()
    mk = ab[ab['common']].groupby(['trade_date', 'market'], observed=True)['ret'].mean().unstack().sort_index()
    return r, mk


def forward(r, mk, codes, markets, sell_dates, horizons=HORIZONS):
    """사건마다 매도일 다음 날부터 H 거래일 초과수익. 상장폐지(데이터 끝 전 시세 끝)는 마지막 날 이후 수익 0."""
    days = r.index
    pos = days.searchsorted(pd.DatetimeIndex(sell_dates))
    col = r.columns.get_indexer(codes)
    R = np.nan_to_num(r.to_numpy(dtype='float64'), nan=0.0)
    logR = np.log1p(np.clip(R, -0.99, None))
    cumS = np.vstack([np.zeros((1, R.shape[1])), logR.cumsum(axis=0)])
    M = {m: np.concatenate([[0.0], np.log1p(mk[m].reindex(days).fillna(0).to_numpy()).cumsum()]) for m in mk.columns}
    out = {}
    for h in horizons:
        ok = (pos < len(days)) & (pos + h < len(days)) & (col >= 0)
        a, b = pos + 1, pos + 1 + h              # 창: 매도일 다음 날 ~ h번째 날 (cum 인덱스는 하루씩 밀려 있다)
        rs = np.full(len(pos), np.nan)
        rm = np.full(len(pos), np.nan)
        rs[ok] = np.expm1(cumS[b[ok], col[ok]] - cumS[a[ok], col[ok]])
        for m in M:
            sel = ok & (np.asarray(markets) == m)
            rm[sel] = np.expm1(M[m][b[sel]] - M[m][a[sel]])
        out[h] = rs - rm
    return out


def boot(values, clusters, rng, n=N_BOOT):
    df = pd.DataFrame({'v': values, 'g': clusters}).dropna()
    s = df.groupby('g')['v'].agg(['sum', 'count'])
    idx = rng.integers(0, len(s), size=(n, len(s)))
    b = s['sum'].to_numpy()[idx].sum(1) / s['count'].to_numpy()[idx].sum(1)
    lo, hi = np.percentile(b, [2.5, 97.5])
    return {'mean': float(df['v'].mean()), 'ci95': [float(lo), float(hi)], 'p_mean_ge_0': float((b >= 0).mean()),
            'median': float(df['v'].median()), 'share_negative': float((df['v'] < 0).mean()),
            'n_events': int(len(df)), 'n_stocks': int(df['g'].nunique())}


def event_study(ev_df, ab, r, mk, q, rng, step=10):
    days = r.index
    ev_df = ev_df.copy()
    sell_pos = days.searchsorted(ev_df['drop_date']) + 1
    ev_df = ev_df[sell_pos < len(days)]
    ev_df['sell_date'] = days[days.searchsorted(ev_df['drop_date']) + 1]
    ev_df['regime'] = regime_of(ev_df['drop_date'])
    fw = forward(r, mk, ev_df['stock_code'].to_numpy(), ev_df['market'].astype(str).to_numpy(), ev_df['sell_date'])
    for h, v in fw.items():
        ev_df[f'ar_{h}d'] = v
    # 상장폐지로 창이 잘린 사건 표시 (종목 마지막 거래일이 창 끝보다 앞)
    last = ab.groupby('stock_code')['trade_date'].max()
    ev_df['delisted_by'] = ev_df['stock_code'].map(last)
    res = {}
    for rule, g in ev_df.groupby('rule'):
        rec = {}
        for h in HORIZONS:
            col = f'ar_{h}d'
            rec[h] = {'전체': boot(g[col], g['stock_code'], rng)}
            for reg, gg in g.groupby('regime'):
                rec[h][reg] = boot(gg[col], gg['stock_code'], rng)
        res[rule] = rec
    # 기준선: 같은 조건(보통주, 시가총액 기준 통과) 종목의 아무 날 (step 거래일마다 표집)
    base = {}
    d_keep = set(days[::step])
    for rule, ok_col in (('abs', 'abs_ok'), ('pct', 'pct_ok')):
        b = ab[ab['common'] & ab['trade_date'].isin(d_keep)].copy()
        b = b[(b['mktcap'] >= ev.MIN_MKTCAP) if rule == 'abs' else (b['mktcap_rank'] <= b['market'].map(q).astype(float))]
        fwb = forward(r, mk, b['stock_code'].to_numpy(), b['market'].astype(str).to_numpy(), b['trade_date'])
        base[rule] = {h: {'mean': float(np.nanmean(v)), 'median': float(np.nanmedian(v)), 'n': int(np.isfinite(v).sum())}
                      for h, v in fwb.items()}
    return ev_df, res, base


# ---- 4. 포트폴리오 ----
def simulate(ab, r, ev_df, q, rule, rng, runs=600, n_pick=20, years=(1, 2, 3)):
    days = r.index
    R = np.nan_to_num(r.to_numpy(dtype='float64'), nan=0.0)
    codes = r.columns
    alive_last = ab.groupby('stock_code')['trade_date'].max().reindex(codes)
    first_drop = ev_df[ev_df['rule'] == rule][['stock_code', 'drop_date']]
    elig = ab[ab['common']]
    elig = elig[(elig['mktcap'] >= ev.MIN_MKTCAP) if rule == 'abs'
                else (elig['mktcap_rank'] <= elig['market'].map(q).astype(float))]
    elig_by_day = elig.groupby('trade_date')['stock_code'].apply(list)
    start_ok = days[ev.SIGMA_WINDOW + 10:]                    # σ가 생긴 뒤부터 진입
    rows = []
    for y in years:
        hz = 252 * y
        cand = start_ok[start_ok <= days[-1 - hz]]
        for k in range(runs):
            entry = cand[rng.integers(len(cand))]
            i0 = days.get_loc(entry)
            pool = elig_by_day.get(entry, [])
            if len(pool) < n_pick:
                continue
            pick = rng.choice(pool, n_pick, replace=False)
            cj = codes.get_indexer(pick)
            seg = R[i0 + 1:i0 + 1 + hz, cj]                    # 진입일 종가에 사서 다음 날부터 수익
            # 상장폐지 뒤는 수익 0 (마지막 가격에서 현금). R이 이미 0으로 채워져 있다
            path = np.cumprod(1 + seg, axis=0)
            hold = path.mean(1)
            # 급락매도: 진입 다음 날 ~ 끝 사이 첫 급락의 다음 거래일 종가
            fd = first_drop[first_drop['stock_code'].isin(pick) & (first_drop['drop_date'] > entry)
                            & (first_drop['drop_date'] < days[i0 + hz])].groupby('stock_code')['drop_date'].min()
            sell_at = np.full(n_pick, hz, dtype=int)            # 창 안 상대 위치(그 날 종가까지 보유)
            for j, c in enumerate(pick):
                if c in fd.index:
                    sp = days.searchsorted(fd[c]) + 1 - (i0 + 1)  # 매도일의 seg 인덱스
                    sell_at[j] = min(sp, hz - 1)
            n_sell = int((sell_at < hz).sum())

            def frozen(sell):
                p = path.copy()
                for j, s in enumerate(sell):
                    if s < hz:
                        p[s:, j] = p[s, j] * (1 - COST)
                return p.mean(1)
            strat = frozen(sell_at)
            rand = []
            for _ in range(3):
                s = np.full(n_pick, hz, dtype=int)
                if n_sell:
                    js = rng.choice(n_pick, n_sell, replace=False)
                    s[js] = rng.integers(0, hz, n_sell)
                rand.append(frozen(s))
            mdd = lambda W: float((1 - W / np.maximum.accumulate(np.concatenate([[1.0], W]))[1:]).max())
            rows.append({'years': y, 'run': k, 'entry': entry.strftime('%Y-%m-%d'), 'regime': regime_of(pd.Series([entry]))[0],
                         'sells': n_sell,
                         'delisted_in_window': int((alive_last.iloc[cj] < days[i0 + hz]).sum()),
                         'hold_ret': float(hold[-1] - 1), 'drop_ret': float(strat[-1] - 1),
                         'rand_ret': float(np.mean([w[-1] for w in rand]) - 1),
                         'hold_mdd': mdd(hold), 'drop_mdd': mdd(strat), 'rand_mdd': float(np.mean([mdd(w) for w in rand]))})
    return pd.DataFrame(rows)


def sim_summary(sim, rng):
    out = {}
    sim = sim.copy()
    sim['quarter'] = pd.to_datetime(sim['entry']).dt.to_period('Q').astype(str)
    for y, g in sim.groupby('years'):
        rec = {}
        for reg, gg in [('전체', g)] + list(g.groupby('regime')):
            d1 = gg['drop_ret'] - gg['hold_ret']
            d2 = gg['drop_ret'] - gg['rand_ret']
            rec[reg] = {'runs': int(len(gg)), 'sells_median': float(gg['sells'].median()),
                        'hold_ret': float(gg['hold_ret'].mean()), 'drop_ret': float(gg['drop_ret'].mean()),
                        'rand_ret': float(gg['rand_ret'].mean()),
                        'hold_mdd': float(gg['hold_mdd'].mean()), 'drop_mdd': float(gg['drop_mdd'].mean()),
                        'rand_mdd': float(gg['rand_mdd'].mean()),
                        '급락-보유': {**boot(d1, gg['quarter'], rng), 'win_rate': float((d1 > 0).mean())},
                        '급락-무작위': {**boot(d2, gg['quarter'], rng), 'win_rate': float((d2 > 0).mean())}}
        out[int(y)] = rec
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--seed', type=int, default=20261122)
    ap.add_argument('--runs', type=int, default=600)
    ap.add_argument('--start', default='2018-12-01', help='이 날짜 이후 일별 파일만 읽는다 (받아 둔 기간)')
    ap.add_argument('--index-csv', type=Path, help='시장지수 파일. 없으면 cache/long_history/index.csv, 그것도 없으면 pykrx')
    ap.add_argument('--refresh-index', action='store_true')
    ap.add_argument('--out', type=Path, default=OUT)
    args = ap.parse_args(argv)
    rng = np.random.default_rng(args.seed)
    p = load_daily(args.start)
    print(f'시세 {len(p):,}행, {p["trade_date"].min():%Y-%m-%d} ~ {p["trade_date"].max():%Y-%m-%d}, 종목 {p["stock_code"].nunique():,}', flush=True)
    index = ev.load_index(p, index_csv=args.index_csv) if args.index_csv else load_index(p, refresh=args.refresh_index)
    p['common'] = common_mask(p)
    q = size_cut(p)
    print('pct 기준 q (2026년 1,000억 이상 비율):', q, flush=True)
    events, ab = drop_events(p.drop(columns='common'), index, q)
    print(events.groupby('rule').size().to_dict(), flush=True)
    r, mk = growth_grids(ab)
    ev_df, es, base = event_study(events, ab, r, mk, q, rng)
    args.out.mkdir(parents=True, exist_ok=True)
    ev_df.to_csv(args.out / 'events.csv', index=False, encoding='utf-8-sig')
    res = {'seed': args.seed, 'period': [f'{p["trade_date"].min():%Y-%m-%d}', f'{p["trade_date"].max():%Y-%m-%d}'],
           'stocks': int(p['stock_code'].nunique()), 'q_pct_rule': q, 'regimes': REGIMES,
           'events': events.groupby('rule').size().to_dict(), 'event_study_ar_vs_equal_weight': es, 'baseline_any_day': base}
    sims = []
    for rule in ('abs', 'pct'):
        s = simulate(ab, r, ev_df, q, rule, rng, runs=args.runs)
        s['rule'] = rule
        sims.append(s)
        res[f'portfolio_{rule}'] = sim_summary(s, rng)
        print(f'포트폴리오 {rule} 완료', flush=True)
    pd.concat(sims).to_csv(args.out / 'portfolio_runs.csv', index=False, encoding='utf-8-sig')
    (args.out / 'long_history.json').write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    print(json.dumps({k: res[k] for k in ('period', 'stocks', 'events', 'q_pct_rule')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
