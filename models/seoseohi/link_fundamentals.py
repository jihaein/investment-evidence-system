"""
link_fundamentals.py — 가격 경보(임계 모델)와 펀더멘털 모델(F-Score·Ohlson) 연결

[what]
가격 경보가 켜지면 F-Score·Ohlson에서도 악화가 확인되는가?
"악화 확인" = 매수일 → 확인일 비교에서 위험점수가 1점 이상 (F-Score 0~3점, Ohlson 0~2점)

[how to compare]
 - 경보가 켜진 날(onset) t마다, 그 보유의 매수일 b에서 t + lead 거래일까지의 위험점수를 본다
     lead 0   = 경보 시점에 오케스트라가 보게 되는 값 (그날까지 공시된 자료만 사용)
     lead 120·250 = 경보 이후 약 반 년·1년 뒤의 값 (백테스트 전용: 미래 자료를 평가에만 쓴다)
 - 비교 기준(expected): 같은 보유 안에서 경보와 비슷한 시점(±30거래일)의 아무 날의 같은 값
     매수 후 경과 시간이 길수록 새 보고서가 나와 점수가 바뀌기 쉬워서, 경과 시간이 같은 날끼리 비교한다
 - diff = 경보일 비율 - 기준 비율, lift = 경보일 비율 / 기준 비율, z = diff / 표준오차 (같은 종목·시기가 겹쳐 있어 대략적인 값)

[warning]]
 - 유보(같은 보고서, 연결↔별도 등)로 점수가 없는 쌍은 계산에서 빠진다. n = 점수가 있는 경보 수
 - 빠른 계산을 쓰고, 시작할 때 공식 함수(run_models.run_fscore·run_ohlson)와 표본 쌍을 대조한다

[execution] python link_fundamentals.py  
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

import run_models as rm
import threshold as T


OUTPUT_DIR = Path(__file__).resolve().parent / "output"

HOLD_HORIZON = 500          # 보유 구간 길이(거래일). 경보는 이 안에서 찾는다
HOLD_STEP = 60              # 매수일을 이 간격으로 바꿔 가며 여러 보유를 만든다 (threshold.py 빈도표와 같음)
CONTROL_STEP = 5            # 기준(아무 날)으로 쓰는 날의 간격
MATCH_WINDOW = 30           # 경보일과 경과 시간이 ±이만큼 비슷한 날을 기준으로 쓴다
LEADS = (0, 120, 250)       # 확인 시점: 경보일로부터 몇 거래일 뒤의 점수를 볼지
SELF_CHECK_PAIRS = 300      # 빠른 계산을 공식 함수와 대조할 표본 쌍 수

# 평가할 (k, h, 조건 3). 앞의 적중률 표에서 경보 수가 충분했던 대표 설정
CONFIGS = [(0.5, 5, "off"), (0.5, 5, "share"), (0.5, 3, "off"), (0.75, 5, "off"),
           (0.5, 8, "off"), (0.25, 8, "off"), (0.1, 5, "off")]


# 1. 날짜별 펀더멘털 점수 조회
#    공식 함수와 같은 규칙: F-Score는 그날까지 공시된 최근 보고서, Ohlson은 그날 행

class FundamentalLookup:
    def __init__(self, by_stock):
        self.fs, self.oh = rm.fscore_module(), rm.ohlson_module()
        self.ri_small = rm.OHLSON_SETTINGS["ri_small"]
        fs_table, oh_table = rm.build_fscore_table(), rm.build_ohlson_table()
        self.f_score, self.f_year, self.o_state, self.o_s, self.o_fs, self.o_ok = {}, {}, {}, {}, {}, {}

        for code, g in by_stock.items():
            dates = pd.DatetimeIndex(g["trade_date"])
            n = len(dates)

            # F-Score: 그날(포함)까지 공시된 가장 최근 사업보고서
            sub = fs_table[fs_table["stock_code"] == code].sort_values("disclosed_date")
            if len(sub):
                idx = np.searchsorted(sub["disclosed_date"].to_numpy(), dates.to_numpy(), side="right") - 1
                safe = np.maximum(idx, 0)
                score = pd.to_numeric(sub["f_score"], errors="coerce").to_numpy(dtype=float, na_value=np.nan)[safe]
                self.f_score[code] = np.where(idx >= 0, score, np.nan)
                self.f_year[code] = np.where(idx >= 0, sub["fiscal_year"].to_numpy()[safe], -1)
            else:
                self.f_score[code], self.f_year[code] = np.full(n, np.nan), np.full(n, -1)

            # Ohlson: 그날의 행
            sub = oh_table[oh_table["stock_code"] == code].sort_values("trade_date")
            pos = pd.Index(sub["trade_date"]).get_indexer(dates) if len(sub) else np.full(n, -1)
            safe = np.maximum(pos, 0)
            if len(sub):
                self.o_state[code] = np.where(pos >= 0, sub["omega_state"].to_numpy(object)[safe], None)
                self.o_s[code] = np.where(pos >= 0, sub["ri_spread"].to_numpy(float)[safe], np.nan)
                self.o_fs[code] = np.where(pos >= 0, sub["fs_div_used"].to_numpy(object)[safe], None)
                self.o_ok[code] = (pos >= 0) & sub["omega_interpretable"].to_numpy(bool)[safe]
            else:
                self.o_state[code], self.o_fs[code] = np.full(n, None, object), np.full(n, None, object)
                self.o_s[code], self.o_ok[code] = np.full(n, np.nan), np.zeros(n, bool)

    # 매수일 b → 확인일 t의 F-Score 위험점수 (유보면 NaN)
    def f_risk(self, code, b, t):
        fb, ft = self.f_score[code][b], self.f_score[code][t]
        if np.isnan(fb) or np.isnan(ft):
            return np.nan
        if self.fs.HOLD_IF_SAME_REPORT and self.f_year[code][b] == self.f_year[code][t]:
            return np.nan
        return float(self.fs.map_risk_score(fb - ft) + self.fs.level_add_points(ft))

    # 매수일 b → 확인일 t의 Ohlson 점수 (유보면 NaN)
    def o_score(self, code, b, t):
        if self.o_fs[code][b] != self.o_fs[code][t]:
            return np.nan                                    # 연결↔별도: basis_changed
        if not (self.o_ok[code][b] and self.o_ok[code][t]):
            return np.nan
        ri_dir = self.oh.direction(self.o_s[code][t] - self.o_s[code][b], self.ri_small)
        omega_dir = self.oh.state_direction(self.o_state[code][b], self.o_state[code][t])
        return float(self.oh.SCORE_TABLE[(ri_dir, omega_dir)])


# 빠른 계산이 공식 함수와 같은 점수를 내는지 표본 쌍으로 대조한다
def self_check(look, by_stock, n_pairs=SELF_CHECK_PAIRS, seed=0):
    rng = np.random.default_rng(seed)
    codes = list(by_stock)
    pairs, idx = [], []
    while len(pairs) < n_pairs:
        code = codes[rng.integers(len(codes))]
        g = by_stock[code]
        if len(g) < 400:
            continue
        b = int(rng.integers(0, len(g) - 200))
        t = int(rng.integers(b + 1, len(g)))
        d = g["trade_date"]
        pairs.append((code, d.iloc[b].strftime("%Y-%m-%d"), d.iloc[t].strftime("%Y-%m-%d")))
        idx.append((code, b, t))

    off_f = rm.run_fscore(pairs)["risk_score"].astype(float).to_numpy()
    off_o = rm.run_ohlson(pairs)["risk_score"].astype(float).to_numpy()
    fast_f = np.array([look.f_risk(*i) for i in idx])
    fast_o = np.array([look.o_score(*i) for i in idx])

    def diff(a, b):
        both_nan = np.isnan(a) & np.isnan(b)
        return int((~(both_nan | (a == b))).sum())

    bad_f, bad_o = diff(fast_f, off_f), diff(fast_o, off_o)
    print(f"[자체 점검] 빠른 계산 vs 공식 함수 {n_pairs}쌍: F-Score 불일치 {bad_f}, Ohlson 불일치 {bad_o}"
          f"  (점수 나온 쌍: F {int((~np.isnan(off_f)).sum())}, O {int((~np.isnan(off_o)).sum())})")
    if bad_f or bad_o:
        print("  → 불일치가 있으면 아래 결과를 믿지 말고 알려 주세요")
    return bad_f + bad_o



# 2. 보유 구간·경보·기준일 만들기

def build_holds(by_stock):
    holds = []
    for code, g in by_stock.items():
        ready = g["sigma"].notna().to_numpy()
        if not ready.any():
            continue
        first = int(np.argmax(ready))
        for b in range(first, len(g) - HOLD_HORIZON, HOLD_STEP):
            holds.append((code, b, b + HOLD_HORIZON))
    return holds


# 경보 이후 확인 시점의 0/1 값 (NaN = 점수 없음): F-Score ≥ 1, Ohlson ≥ 1, 둘 중 하나라도 ≥ 1
def outcomes(look, cache, code, b, t, lead, n):
    t2 = t + lead
    if t2 >= n:
        return np.nan, np.nan, np.nan
    key = (code, b, t2)
    if key not in cache:
        f, o = look.f_risk(code, b, t2), look.o_score(code, b, t2)
        bf = np.nan if np.isnan(f) else float(f >= 1)
        bo = np.nan if np.isnan(o) else float(o >= 1)
        cache[key] = (bf, bo, np.fmax(bf, bo))
    return cache[key]


def matched_mean(ctrl_idx, ctrl_vals, t):
    lo = np.searchsorted(ctrl_idx, t - MATCH_WINDOW, side="left")
    hi = np.searchsorted(ctrl_idx, t + MATCH_WINDOW, side="right")
    v = ctrl_vals[lo:hi]
    v = v[~np.isnan(v)]
    return float(v.mean()) if len(v) else np.nan


def stats(y, e):
    y, e = np.asarray(y, float), np.asarray(e, float)
    ok = ~np.isnan(y) & ~np.isnan(e)
    n = int(ok.sum())
    if n < 2:
        return n, np.nan, np.nan, np.nan, np.nan, np.nan
    y, e = y[ok], e[ok]
    d = y - e
    se = d.std(ddof=1) / np.sqrt(n)
    obs, exp = y.mean(), e.mean()
    return n, obs, exp, obs - exp, (obs - exp) / se if se > 0 else np.nan, obs / exp if exp > 0 else np.nan


def analyze(by_stock, look, holds):
    cache = {}
    rows = []
    # 기준일: 보유마다 CONTROL_STEP 간격의 날 (점수판이 계산 가능한 날만)
    controls = {}
    for hid, (code, b, e) in enumerate(holds):
        g = by_stock[code]
        sig = g["sigma"].notna().to_numpy()
        controls[hid] = np.array([c for c in range(b + 1, e + 1, CONTROL_STEP) if sig[c]], dtype=int)

    for k, h, c3 in CONFIGS:
        onset_by_hold = {}
        for hid, (code, b, e) in enumerate(holds):
            res = T.run_cusum(by_stock[code], b, e, k, h, "moving", c3)
            onset_by_hold[hid] = np.flatnonzero(res["onset"] & res["live"])
        n_onsets = sum(len(v) for v in onset_by_hold.values())

        for lead in LEADS:
            y = {m: [] for m in ("F", "O", "either")}
            ex = {m: [] for m in ("F", "O", "either")}
            for hid, (code, b, e) in enumerate(holds):
                ons = onset_by_hold[hid]
                if not len(ons):
                    continue
                n = len(by_stock[code])
                ci = controls[hid]
                if not len(ci):
                    continue
                cv = np.array([outcomes(look, cache, code, b, int(c), lead, n) for c in ci])    # (제어일 수, 3)
                for t in ons:
                    ov = outcomes(look, cache, code, b, int(t), lead, n)
                    for j, m in enumerate(("F", "O", "either")):
                        y[m].append(ov[j])
                        ex[m].append(matched_mean(ci, cv[:, j], int(t)))
            for m, label in (("F", "F-Score"), ("O", "Ohlson"), ("either", "둘 중 하나")):
                n, obs, exp, diff, z, lift = stats(y[m], ex[m])
                rows.append({"k": k, "h": h, "cond3": c3, "lead": lead, "measure": label, "onsets": n_onsets,
                             "n": n, "alarm_rate": obs, "expected_rate": exp, "diff": diff, "z": z, "lift": lift})
    return pd.DataFrame(rows)



# 3. 실행

if __name__ == "__main__":
    by_stock = T.prepared_prices()
    look = FundamentalLookup(by_stock)
    bad = self_check(look, by_stock)

    holds = build_holds(by_stock)
    print(f"[보유 구간] {len(holds)}개 (보유 {HOLD_HORIZON}거래일, 매수일 {HOLD_STEP}거래일 간격) / 설정 {len(CONFIGS)}개 × 확인 시점 {LEADS}")
    print("  악화 확인 = 매수일 → 확인일 위험점수 1점 이상. lead 0 = 경보일 당일, 120·250 = 그 뒤 (미래 자료, 평가 전용)")
    print("  alarm_rate = 경보일의 악화 확인 비율, expected_rate = 경과 시간이 비슷한 아무 날의 비율, diff = 차이, lift = 비, z = 차이/표준오차")

    table = analyze(by_stock, look, holds)
    show = table.drop(columns=["onsets"]).copy()
    print(show.round(3).to_string(index=False))

    OUTPUT_DIR.mkdir(exist_ok=True)
    table.to_csv(OUTPUT_DIR / "threshold_fundamental_link.csv", index=False, encoding="utf-8-sig")
    print("\n저장 완료:", OUTPUT_DIR)
    if bad:
        sys.exit(1)