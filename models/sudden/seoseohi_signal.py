"""서서히 모델(팀 models/seoseohi/pipeline.run_seoseohi) 신호를 급락 백테스트에 쓰기 위한 어댑터 (19종목 2016~2019).

- 평가일: 매년 4/1·6/1·9/1·12/1 (정기 보고서 공시 마감 뒤). 19종목 본데이터는 공시일 기준으로 그날까지 나온 보고서만 쓴다.
- 기준선(buy_date): 진입일 뒤 평가일 중, 다음 평가일 판정이 "유보"가 아닌 첫 날 (종목마다).
- 신호: 기준선 뒤 평가일에서 트리거("Yes(훼손 우려)")가 처음 나온 날. 개수 방식(integrate)과 합산 방식(integrate_sum).
- 입력 경로는 환경변수 SEOSEOHI_TTM_DIR, SEOSEOHI_FINAL_DATA_DIR (팀 Google Drive). .env를 바꾸지 않고 실행할 때 넘긴다.
"""
import sys
from functools import lru_cache
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
TRIGGER = 'Yes(훼손 우려)'


def eval_dates(start='2016-01-01', end='2019-12-31'):
    out = []
    for y in range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1):
        for md in ('04-01', '06-01', '09-01', '12-01'):
            d = pd.Timestamp(f'{y}-{md}')
            if pd.Timestamp(start) <= d <= pd.Timestamp(end):
                out.append(d)
    return out


class Seoseohi:
    """run_seoseohi 결과를 (종목, 기준선, 평가일) 단위로 저장해 같은 계산을 반복하지 않는다."""

    def __init__(self, runner=None):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from models.seoseohi.pipeline import integrate, integrate_sum
        self._integrate, self._integrate_sum = integrate, integrate_sum
        if runner is None:
            from models.seoseohi.pipeline import load_data, run_seoseohi
            data = load_data()

            def runner(code, buy, now):
                r = run_seoseohi(data, {'stock_code': code, 'buy_date': buy}, now)
                return {k: r[k]['score'] for k in ('dupont', 'fscore', 'ohlson')}
        self.runner = runner
        self.cache = {}

    def scores(self, code, buy, now):
        key = (code, buy, now)
        if key not in self.cache:
            self.cache[key] = self.runner(code, buy.strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d'))
        return self.cache[key]

    def verdict(self, code, buy, now, mode):
        s = self.scores(code, buy, now)
        return (self._integrate if mode == 'count' else self._integrate_sum)(s)

    def first_signals(self, codes, entry, end, dates=None):
        """{'count': {종목: 신호일}, 'sum': {...}}. 기준선을 못 찾거나 신호가 없으면 그 종목은 빠진다."""
        dates = [d for d in (dates or eval_dates()) if d > pd.Timestamp(entry)]
        out = {'count': {}, 'sum': {}}
        for code in codes:
            base = None
            for i, d in enumerate(dates[:-1]):
                if self.verdict(code, d, dates[i + 1], 'count') != '유보':
                    base = i
                    break
            if base is None:
                continue
            for mode in ('count', 'sum'):
                for d in dates[base + 1:]:
                    if d > pd.Timestamp(end):
                        break
                    if self.verdict(code, dates[base], d, mode) == TRIGGER:
                        out[mode][code] = d
                        break
        return out
