"""KRX 전종목 일별 시세와 시장지수를 pykrx로 받아 events.py가 읽는 형식으로 저장한다 (2023~2025 표본용).

- 날짜마다 코스피(STK)·코스닥(KSQ) 전종목 시세를 받는다. 하루치는 cache/krx/daily/에 저장해 다시 받지 않는다.
- KRX 로그인 정보(.env의 KRX_ID, KRX_PW)를 pykrx가 사용한다.
- 출력 열은 기존 05번 주가 파일과 같다(BAS_DD, ISU_CD(단축코드), ISU_NM, MKT_NM, SECT_TP_NM, 종가, 등락률, 거래량, 시가총액 등).
- KRX 자료는 재배포하지 않으므로 cache 아래에만 둔다.

    python models/sudden/krx_prices.py --start 2022-06-01 --end 2025-12-31
"""
import argparse
import time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = HERE / 'cache/krx'
COLUMNS = ['BAS_DD', 'ISU_CD', 'ISU_NM', 'MKT_NM', 'SECT_TP_NM', 'TDD_CLSPRC', 'CMPPREVDD_PRC', 'FLUC_RT',
           'TDD_OPNPRC', 'TDD_HGPRC', 'TDD_LWPRC', 'ACC_TRDVOL', 'ACC_TRDVAL', 'MKTCAP', 'LIST_SHRS']
INDEX = {'KOSPI': '1001', 'KOSDAQ': '2001'}


def load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE.parents[1] / '.env')
    except ImportError:
        pass


def fetch_day(day, fetcher=None):
    """하루치 두 시장 시세. 휴장일이면 빈 표."""
    path = OUT / 'daily' / f'{day:%Y%m%d}.csv'
    if path.exists():
        return pd.read_csv(path, dtype=str, encoding='utf-8-sig')
    if fetcher is None:
        from pykrx.website.krx.market.core import 전종목시세
        fetcher = 전종목시세().fetch
    frames = []
    for mkt in ('STK', 'KSQ'):
        df = fetcher(f'{day:%Y%m%d}', mkt)
        if df is None or not len(df) or df['TDD_CLSPRC'].replace('-', '').eq('').all():
            continue
        df = df.rename(columns={'ISU_CD': 'ISU_FULL', 'ISU_SRT_CD': 'ISU_CD', 'ISU_ABBRV': 'ISU_NM'})
        df['BAS_DD'] = f'{day:%Y%m%d}'
        frames.append(df[COLUMNS])
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False, encoding='utf-8-sig')
    return out


def fetch_index(start, end):
    from pykrx import stock
    get = getattr(stock, 'get_index_ohlcv', None) or getattr(stock, 'get_index_ohlcv_by_date')
    frames = []
    for market, ticker in INDEX.items():
        df = get(f'{start:%Y%m%d}', f'{end:%Y%m%d}', ticker)
        frames.append(pd.DataFrame({'trade_date': pd.to_datetime(df.index).strftime('%Y-%m-%d'), 'market': market,
                                    'close': pd.to_numeric(df['종가'], errors='coerce').to_numpy()}))
    return pd.concat(frames, ignore_index=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--start', default='2022-06-01')
    ap.add_argument('--end', default='2025-12-31')
    ap.add_argument('--pause', type=float, default=0.5)
    args = ap.parse_args(argv)
    load_env()
    start, end = pd.Timestamp(args.start), pd.Timestamp(args.end)
    days = pd.bdate_range(start, end)
    frames = []
    for i, day in enumerate(days):
        cached = (OUT / 'daily' / f'{day:%Y%m%d}.csv').exists()
        frames.append(fetch_day(day))
        if not cached:
            time.sleep(args.pause)
        if i % 50 == 0:
            print(f'{day:%Y-%m-%d} ({i + 1}/{len(days)})', flush=True)
    prices = pd.concat([f for f in frames if len(f)], ignore_index=True)
    # 기존 05번 파일처럼 두 번째 줄에 한국어 열 이름이 있어도 events.load_prices는 날짜 형식으로 거른다
    prices.to_csv(OUT / f'prices_{start:%Y%m%d}_{end:%Y%m%d}.csv', index=False, encoding='utf-8-sig')
    index = fetch_index(start - pd.Timedelta(days=14), end)
    index.to_csv(OUT / f'index_{start:%Y%m%d}_{end:%Y%m%d}.csv', index=False, encoding='utf-8-sig')
    print(f'시세 {len(prices)}행, 거래일 {prices["BAS_DD"].nunique()}일, 종목 {prices["ISU_CD"].nunique()}개 → {OUT}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
