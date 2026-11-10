"""3차 ACH 시장 규칙 단위 (설계서 v3.1 5.3절). LLM을 거치지 않고 가격 데이터로 코드가 직접 만든다. 가중치 0.6.

지금 만들 수 있는 규칙
- K4 급락 전 과열: D-20 ~ D-1 누적 AR이 그 종목의 직전 1년(D-260 ~ D-21에 끝나는) 20거래일 누적 AR 분포에서 상위 5%.
  AR은 시장조정(r - r_m)이다(events.py와 같은 정의).

아직 못 만드는 규칙
- K1a·K1b 업종 동반/비동반: 종목별 업종지수 연결 자료가 없다.
- K3 신용 정황: 종목별 신용잔고율 자료가 없다.
- K2 공시 후 갭 하락: 공시 시각(KIND)이 없다.
"""
import numpy as np

K_WEIGHT = 0.6
K4_WINDOW, K4_HISTORY, K4_QUANTILE = 20, 240, 0.95


def k4(stock, d_index):
    """stock: 거래일순 DataFrame(ret, index_ret, halted). 과열이면 단위 dict, 아니면 None. 자료 부족이면 'insufficient'."""
    ar = (stock['ret'] - stock['index_ret']).where(~stock['halted']).fillna(0.0).to_numpy(float)
    if d_index < K4_WINDOW + K4_HISTORY:
        return 'insufficient'
    recent = ar[d_index - K4_WINDOW:d_index].sum()
    sums = np.convolve(ar[d_index - K4_WINDOW - K4_HISTORY:d_index - K4_WINDOW], np.ones(K4_WINDOW), 'valid')
    cut = float(np.quantile(sums, K4_QUANTILE))
    if recent <= cut:
        return None
    return {'type': 'K4', 'direction': '없음', 'w': K_WEIGHT, 'quote': f'D-20~D-1 누적 AR {recent:.1%} (1년 상위 5% 기준 {cut:.1%})',
            'event': 'K4 급락 전 과열', 'market_rule': True, 'car_pre': float(recent), 'cut': cut}


def event_units(events):
    """사건별 시장 규칙 단위 목록. 가격 자료를 한 번만 읽는다."""
    import events as ev
    prices = ev.load_prices()
    index = ev.load_index(prices)
    df = prices.merge(index, on=['trade_date', 'market'], how='left')
    df['halted'] = df['volume'].fillna(0) == 0
    by_stock = {c: g.sort_values('trade_date').reset_index(drop=True) for c, g in df.groupby('stock_code')}
    out = {}
    for e in events:
        stock = by_stock.get(e['stock_code'])
        hit = [] if stock is None else stock.index[stock['trade_date'] == np.datetime64(e['D'])]
        if not len(hit):
            out[e['drop_id']] = {'status': 'no_price', 'units': []}
            continue
        u = k4(stock, int(hit[0]))
        out[e['drop_id']] = {'status': 'insufficient' if u == 'insufficient' else 'ok',
                             'units': [u] if isinstance(u, dict) else []}
    return out
