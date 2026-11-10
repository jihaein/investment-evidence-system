"""
events.py — 급락 모델 1단계: 급락 사건 목록 

[what]
19종목 주가(05번)와 시장지수로 급락일을 찾고, 사건마다 "판단 시각까지 나온 뉴스만 쓴다"는
시간 경계를 함께 적어 둔다. 뉴스 수집·필터·판정은 모두 이 표의 시간 칸만 보고 자른다.

[급락 정의] 
    AR = 종목 수익률 - 시장지수 수익률 (코스닥 종목은 코스닥 지수, 코스피 종목은 코스피 지수)
    z  = AR / σ,  σ = D-120 ~ D-1 거래일 AR의 표준편차
    z <= -1.96 이면 급락일
    - 같은 종목이 첫 급락일로부터 10거래일 안에 다시 급락하면 같은 사건(drop_id)으로 묶고 첫날을 D로 한다
    - 제외: 권리락·배당락·분할·병합 등 보정일(등락률과 종가 수익률이 0.5%p 넘게 다른 날), 거래정지 해제 첫날
    - 시가총액 1,000억 원 기준은 D-1 시가총액으로 표시만 하고 기본은 거르지 않는다 (19종목 대부분이 미만. 팀 결정)
    - 관리종목 여부는 자료가 없어 확인하지 못한다. 상장폐지 20거래일 이내는 near_delisting으로 표시한다

[시간 경계] (그 당시 투자자가 볼 수 있었던 정보만 쓴다)
    판단 시각 t0 = D일 15:30 (장 마감). 이 시각 이후에 나온 기사·공시는 이 사건에 쓰지 않는다
    세션 k = (직전 거래일 15:30, k일 15:30]   → 장 마감 후 기사는 다음 거래일 세션
    뉴스 창 = D-2 세션 시작 ~ t0 (세션 3개)  = (D-3 거래일 15:30, D일 15:30]
    신규성 N 기준 기사 = 뉴스 창 시작 전 180일, 파급력 P 기준 = 뉴스 창 시작 전 90일 (둘 다 뉴스 창 시작에서 끝남)
    σ는 D-1까지의 자료만 쓰고, 시가총액도 D-1 값을 쓴다

[시장지수]
    처음 실행하면 pykrx로 받아 cache/index_daily.csv에 저장하고 다음부터 그 파일을 쓴다 (cache는 깃에 안 올라감)
    pykrx를 못 쓰면 --index-csv로 파일을 주거나(열: trade_date, market, close), --no-market으로 시장 조정 없이 근사한다

[실행]
    python models/sudden/events.py
    python models/sudden/events.py --no-market          # 시장지수 없이 근사 (market_adjusted = False)
    python models/sudden/events.py --apply-mktcap       # 시가총액 1,000억 원 미만 사건을 거른다

[결과] models/sudden/output/
    drop_events.csv      사건 표 (사건마다 판단 시각, 뉴스 창, 기준 기간)
    drop_sessions.csv    사건별 세션 3개의 시작·끝 (뉴스를 세션에 배정할 때 사용)
    drop_candidates.csv  z <= -1.96인 모든 날과 제외 사유 (점검용)
    stock_names.csv      종목별 기간별 이름 (뉴스는 그 당시 이름으로 검색한다. 예: 050540 엠피씨 → 한국코퍼레이션)
"""

import os
import argparse
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent                       # models/sudden
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "models" / "seoseohi"))
import threshold as T  # noqa: E402  (주가 파일 경로와 숫자 변환을 같이 쓴다)

PRICE_PATH = Path(os.getenv("SUDDEN_PRICES", T.PRICE_PATH))   # 다른 기간 주가 파일을 쓸 때 바꾼다
CACHE_DIR = HERE / "cache"
INDEX_CACHE = Path(os.getenv("SUDDEN_INDEX", CACHE_DIR / "index_daily.csv"))
OUTPUT_DIR = HERE / os.getenv("SUDDEN_OUTPUT", "output")     # 다른 표본(예: output_2023)을 쓸 때 바꾼다

# ---- 설정 (설계서 v3 2절. 모두 초기값) ----
EVENT_START, EVENT_END = "2016-01-01", "2019-12-31"   # 사건을 찾는 기간 (2015년 주가는 σ 계산에만 쓴다)
Z_CUT = -1.96
SIGMA_WINDOW = 120                  # D-120 ~ D-1 거래일
MIN_OBS = 100                       # σ에 필요한 최소 관측 수 (거래정지일이 빠져서 120보다 조금 낮게)
GROUP_DAYS = 10                     # 첫 급락일로부터 10거래일 안의 재급락은 같은 사건
MIN_MKTCAP = 1000e8                 # 1,000억 원 (D-1 시가총액)
APPLY_MKTCAP = False                # 기본은 표시만 한다 (19종목 대부분 미만. 팀 결정)
RET_GAP_WARN = T.RET_GAP_WARN       # 등락률과 종가 수익률 차이가 이보다 크면 보정일로 본다 (0.5%p)
DECISION_TIME = "15:30"             # 판단 시각 = 장 마감
NEWS_SESSIONS = 3                   # 뉴스 창 세션 수 (D, D-1, D-2)
N_BASE_DAYS = 180                   # 신규성 기준 기간 (일)
P_BASE_DAYS = 90                    # 파급력 기준 기간 (일)
NEAR_DELISTING_DAYS = 20            # 상장폐지 전 이 거래일 안이면 표시
KNOWN_DELISTINGS = {"052270": "2017-04-12"}   # 우전앤한단 (fscore.py의 DELISTED와 같다)
INDEX_TICKERS = {"KOSDAQ": "2001", "KOSPI": "1001"}
TZ = "+09:00"



# 1. read
def market_of(name):
    s = str(name).upper()
    if "KOSDAQ" in s or "코스닥" in s:
        return "KOSDAQ"
    if "KOSPI" in s or "유가" in s or "코스피" in s:
        return "KOSPI"
    return s


def load_prices(path=PRICE_PATH):
    raw = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    raw.columns = [c.strip() for c in raw.columns]
    raw = raw[raw["BAS_DD"].str.fullmatch(r"\d{8}", na=False)]
    prices = pd.DataFrame({
        "stock_code": raw["ISU_CD"].str.strip(),
        "company_name": raw["ISU_NM"].str.strip(),
        "market": raw["MKT_NM"].map(market_of),
        "trade_date": pd.to_datetime(raw["BAS_DD"], format="%Y%m%d"),
        "close": T.to_number(raw["TDD_CLSPRC"]),
        "ret": T.to_number(raw["FLUC_RT"]) / 100,              # 거래소 등락률 (보정일에도 정상 수익률)
        "volume": T.to_number(raw["ACC_TRDVOL"]),
        "mktcap": T.to_number(raw["MKTCAP"]),
        "sect": raw["SECT_TP_NM"].fillna("").str.strip() if "SECT_TP_NM" in raw else "",
    })
    return prices.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)


def fetch_index_pykrx(markets, start, end):
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")                              # KRX 로그인 정보가 .env에 있으면 pykrx가 쓸 수 있게
    except ImportError:
        pass
    try:
        from pykrx import stock
    except ImportError:
        raise RuntimeError("pykrx가 설치돼 있지 않습니다. 'python -m pip install pykrx' 후 다시 실행하거나, "
                           "'--no-market'으로 시장지수 없이 실행하세요.") from None
    get = getattr(stock, "get_index_ohlcv", None) or getattr(stock, "get_index_ohlcv_by_date")
    frames = []
    for m in markets:
        df = get(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), INDEX_TICKERS[m])
        if df is None or len(df) == 0 or "종가" not in df.columns:
            raise RuntimeError(f"pykrx에서 {m} 지수({INDEX_TICKERS[m]})를 받지 못했습니다")
        frames.append(pd.DataFrame({"trade_date": pd.to_datetime(df.index), "market": m,
                                    "close": pd.to_numeric(df["종가"], errors="coerce").to_numpy()}))
    return pd.concat(frames, ignore_index=True)


# 시장지수 종가 → 일별 수익률. 파일을 주면 그 파일, 없으면 캐시, 캐시도 없으면 pykrx로 받는다
def load_index(prices, index_csv=None, refresh=False):
    markets = sorted(prices["market"].unique())
    if index_csv:
        idx = pd.read_csv(index_csv, encoding="utf-8-sig")
    elif INDEX_CACHE.exists() and not refresh:
        idx = pd.read_csv(INDEX_CACHE, encoding="utf-8-sig")
    else:
        start = pd.Timestamp(prices["trade_date"].min()) - timedelta(days=14)
        idx = fetch_index_pykrx(markets, start, prices["trade_date"].max())
        CACHE_DIR.mkdir(exist_ok=True)
        idx.to_csv(INDEX_CACHE, index=False, encoding="utf-8-sig")
    idx["trade_date"] = pd.to_datetime(idx["trade_date"])
    idx["market"] = idx["market"].map(market_of)
    missing = set(markets) - set(idx["market"])
    if missing:
        raise RuntimeError(f"시장지수에 {sorted(missing)}가 없습니다")
    idx = idx.sort_values(["market", "trade_date"])
    idx["index_ret"] = idx.groupby("market")["close"].pct_change()
    return idx[["trade_date", "market", "index_ret"]]



# 2. 급락일 찾기
def add_abnormal(prices, index=None, model="market_adjusted"):
    """model: market_adjusted(v3, AR = r - r_m) 또는 market_model(v3.1, D-120~D-1 회귀 α·β의 잔차와 잔차 표준편차)."""
    df = prices.copy()
    if index is not None:
        df = df.merge(index, on=["trade_date", "market"], how="left")
    else:
        df["index_ret"] = 0.0                                  # --no-market: 시장 조정 없이 근사
    parts = []
    for _, g in df.groupby("stock_code", sort=False):
        g = g.sort_values("trade_date").reset_index(drop=True).copy()
        g["halted"] = g["volume"].fillna(0) == 0
        g["ret_close"] = g["close"].pct_change()
        g["corp_action"] = ((g["ret"] - g["ret_close"]).abs() > RET_GAP_WARN) & ~g["halted"]
        g["after_halt"] = g["halted"].shift(1, fill_value=False) & ~g["halted"]
        if model == "market_model":
            r, m = g["ret"].where(~g["halted"]), g["index_ret"].where(~g["halted"])
            roll = lambda s: s.rolling(SIGMA_WINDOW, min_periods=MIN_OBS)
            cov = roll(r).cov(m).shift(1)                   # D-120 ~ D-1만
            var_m, var_r = roll(m).var().shift(1), roll(r).var().shift(1)
            beta = cov / var_m
            alpha = roll(r).mean().shift(1) - beta * roll(m).mean().shift(1)
            g["beta"] = beta
            g["ar"] = r - (alpha + beta * g["index_ret"])
            g["sigma"] = (var_r - cov ** 2 / var_m).clip(lower=0) ** 0.5
        else:
            g["ar"] = g["ret"].where(~g["halted"]) - g["index_ret"]
            g["sigma"] = g["ar"].rolling(SIGMA_WINDOW, min_periods=MIN_OBS).std().shift(1)   # D-120 ~ D-1만
        g["z"] = g["ar"] / g["sigma"]
        g["mktcap_prev"] = g["mktcap"].shift(1)                                          # D-1 시가총액
        g["row"] = np.arange(len(g))
        parts.append(g)
    return pd.concat(parts, ignore_index=True)


def exclusion_reason(row, apply_mktcap, common_only=False):
    reasons = []
    if common_only:                                         # v3.1: 보통주만, 관리종목 제외
        code, name, sect = str(row["stock_code"]), str(row["company_name"]), str(row.get("sect", ""))
        if not code.endswith("0") or "스팩" in name or "리츠" in name or name.endswith(("우", "우B", "우C")):
            reasons.append("not_common_stock")
        if "관리" in sect:
            reasons.append("administrative_issue")
    if row["corp_action"]:
        reasons.append("corp_action_day")                     # 권리락·배당락·분할·병합 의심일
    if row["after_halt"]:
        reasons.append("first_day_after_halt")
    if apply_mktcap and not (row["mktcap_prev"] >= MIN_MKTCAP):
        reasons.append("mktcap_below_1000eok")
    return "|".join(reasons)


def trading_calendar(prices, index=None):
    dates = index["trade_date"] if index is not None else prices["trade_date"]
    return pd.DatetimeIndex(sorted(pd.unique(pd.to_datetime(dates))))


def at_close(day):
    return f"{pd.Timestamp(day):%Y-%m-%d}T{DECISION_TIME}:00{TZ}"


def stock_names(prices):
    g = prices.groupby(["stock_code", "company_name"])["trade_date"].agg(["min", "max"]).reset_index()
    g = g.sort_values(["stock_code", "min"]).rename(columns={"min": "from", "max": "to"})
    g["from"], g["to"] = g["from"].dt.strftime("%Y-%m-%d"), g["to"].dt.strftime("%Y-%m-%d")
    return g.reset_index(drop=True)


# σ가 모자라(거래정지가 길었거나 상장 직후) 판단하지 못한 큰 하락일 (조용히 빠지지 않게 따로 보여 준다)
def unjudged_big_drops(df, start=EVENT_START, end=EVENT_END, cut=-0.10):
    m = df["sigma"].isna() & (df["ret"] <= cut) & df["trade_date"].between(start, end)
    out = df.loc[m, ["stock_code", "company_name", "trade_date", "ret"]].copy()
    out["trade_date"] = out["trade_date"].dt.strftime("%Y-%m-%d")
    return out.reset_index(drop=True)


def detect_events(prices, index=None, apply_mktcap=APPLY_MKTCAP, start=EVENT_START, end=EVENT_END,
                  model="market_adjusted", common_only=False):
    df = add_abnormal(prices, index, model)
    cal = trading_calendar(prices, index)
    hits = df[(df["z"] <= Z_CUT) & df["trade_date"].between(start, end)].copy()
    hits["excluded"] = [exclusion_reason(r, apply_mktcap, common_only) for _, r in hits.iterrows()]
    hits["drop_id"] = ""

    events, sessions = [], []
    for code, g in hits[hits["excluded"] == ""].groupby("stock_code", sort=False):
        g = g.sort_values("row")
        start, start_id = None, None
        for i, r in g.iterrows():
            if start is not None and r["row"] - start["row"] <= GROUP_DAYS:
                hits.at[i, "drop_id"] = start_id                # 같은 사건
                events[-1]["n_hit_days"] += 1
                events[-1]["last_hit_date"] = r["trade_date"].strftime("%Y-%m-%d")
                continue
            start, start_id = r, f"{code}_{r['trade_date']:%Y%m%d}"
            hits.at[i, "drop_id"] = start_id
            D = r["trade_date"]
            p = cal.get_indexer([D])[0]
            win_start = at_close(cal[p - NEWS_SESSIONS]) if p >= NEWS_SESSIONS else None
            base_end = pd.Timestamp(cal[p - NEWS_SESSIONS]) + timedelta(hours=15, minutes=30) if p >= NEWS_SESSIONS else None
            delist = KNOWN_DELISTINGS.get(code)
            near = False
            if delist is not None:
                d_pos = cal.searchsorted(pd.Timestamp(delist))
                near = 0 <= d_pos - p <= NEAR_DELISTING_DAYS
            events.append({
                "drop_id": start_id, "stock_code": code, "company_name": r["company_name"], "market": r["market"],
                "D": D.strftime("%Y-%m-%d"), "ret_pct": round(r["ret"] * 100, 2),
                "index_ret_pct": round(r["index_ret"] * 100, 2) if pd.notna(r["index_ret"]) else None,
                "ar_pct": round(r["ar"] * 100, 2), "sigma_ar_pct": round(r["sigma"] * 100, 3), "z": round(r["z"], 3),
                "n_hit_days": 1, "last_hit_date": D.strftime("%Y-%m-%d"),
                "mktcap_prev_eok": round(r["mktcap_prev"] / 1e8, 1) if pd.notna(r["mktcap_prev"]) else None,
                "mktcap_ok": bool(r["mktcap_prev"] >= MIN_MKTCAP) if pd.notna(r["mktcap_prev"]) else None,
                "near_delisting": near, "market_adjusted": index is not None, "ar_model": model,
                "decision_time": at_close(D),                   # 판단 시각 t0: 이 시각 이후 정보는 쓰지 않는다
                "news_window_start": win_start,                 # 이 시각 '초과'부터 (D-3 거래일 15:30)
                "news_window_end": at_close(D),                 # 이 시각 '이하'까지 (= 판단 시각)
                "n_base_start": f"{base_end - timedelta(days=N_BASE_DAYS):%Y-%m-%dT%H:%M:%S}{TZ}" if base_end is not None else None,
                "p_base_start": f"{base_end - timedelta(days=P_BASE_DAYS):%Y-%m-%dT%H:%M:%S}{TZ}" if base_end is not None else None,
                "base_end": win_start,                          # 기준 기간은 뉴스 창 시작에서 끝난다 (겹치지 않음)
            })
            for d in range(NEWS_SESSIONS):
                if p - d - 1 < 0:
                    continue
                sessions.append({"drop_id": start_id, "d": d, "trade_date": cal[p - d].strftime("%Y-%m-%d"),
                                 "session_start": at_close(cal[p - d - 1]),   # 초과
                                 "session_end": at_close(cal[p - d])})        # 이하
    cand_cols = ["stock_code", "company_name", "trade_date", "ret", "index_ret", "ar", "sigma", "z",
                 "mktcap_prev", "excluded", "drop_id"]
    candidates = hits[cand_cols].sort_values(["stock_code", "trade_date"]).reset_index(drop=True)
    candidates["trade_date"] = candidates["trade_date"].dt.strftime("%Y-%m-%d")
    return pd.DataFrame(events), pd.DataFrame(sessions), candidates



# 3. execution
KNOWN_CASES = [("053660", "2018-12-19"), ("096350", "2018-05-25"), ("010170", "2018-02-28"), ("065060", "2017-02-02")]

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="급락 사건 목록 만들기 (설계서 v3 2절)")
    ap.add_argument("--index-csv", help="시장지수 파일 (열: trade_date, market, close)")
    ap.add_argument("--refresh-index", action="store_true", help="캐시를 무시하고 pykrx로 다시 받는다")
    ap.add_argument("--no-market", action="store_true", help="시장지수 없이 근사 (시장 조정 안 함)")
    ap.add_argument("--apply-mktcap", action="store_true", help="D-1 시가총액 1,000억 원 미만을 거른다")
    ap.add_argument("--start", default=EVENT_START, help="사건을 찾을 시작일 (기본 2016-01-01)")
    ap.add_argument("--end", default=EVENT_END, help="사건을 찾을 마지막 날 (기본 2019-12-31)")
    ap.add_argument("--v31", action="store_true", help="설계서 v3.1: 시장모형 AR, 시가총액 1,000억 이상, 보통주만, 관리종목 제외")
    args = ap.parse_args()

    prices = load_prices()
    index = None if args.no_market else load_index(prices, args.index_csv, args.refresh_index)
    model = "market_model" if args.v31 else "market_adjusted"
    events, sessions, candidates = detect_events(prices, index, apply_mktcap=args.apply_mktcap or args.v31 or APPLY_MKTCAP,
                                                 start=args.start, end=args.end, model=model, common_only=args.v31)
    names = stock_names(prices)
    unjudged = unjudged_big_drops(add_abnormal(prices, index, model), args.start, args.end)

    print(f"[기간] {args.start} ~ {args.end}")
    print(f"[설정] 시장 조정 {'예' if index is not None else '아니요 (근사)'} / z <= {Z_CUT} / σ: D-{SIGMA_WINDOW} ~ D-1 "
          f"/ 묶음 {GROUP_DAYS}거래일 / 판단 시각 {DECISION_TIME} / 시가총액 거르기 {'예' if (args.apply_mktcap or args.v31) else '아니요(표시만)'}")
    if index is not None:
        miss = prices.merge(index, on=["trade_date", "market"], how="left")["index_ret"].isna().sum()
        print(f"시장지수가 없는 날: {miss}행 (첫날 등)")
    print(f"z <= {Z_CUT}인 날: {len(candidates)}  / 제외: {(candidates['excluded'] != '').sum()}  "
          f"{candidates.loc[candidates['excluded'] != '', 'excluded'].value_counts().to_dict()}")
    print(f"급락 사건: {len(events)}건 (여러 날이 묶인 사건 {int((events['n_hit_days'] > 1).sum())}건)")
    if len(events):
        events["year"] = events["D"].str[:4]
        print("연도별:", events["year"].value_counts().sort_index().to_dict())
        label = names.groupby("stock_code")["company_name"].agg("/".join)
        print("종목별:", {f"{c} {label[c]}": n for c, n in events.groupby("stock_code").size().sort_values(ascending=False).items()})
        print(f"D-1 시가총액 1,000억 원 이상: {int(events['mktcap_ok'].fillna(False).sum())}건 / 상장폐지 임박 표시: {int(events['near_delisting'].sum())}건")
        for code, day in KNOWN_CASES:
            hit = events[(events["stock_code"] == code) & (events["D"] == day)]
            print(f"  알려진 급락 {code} {day}: {'있음 (z ' + str(hit['z'].iloc[0]) + ')' if len(hit) else '없음'}")
        events = events.drop(columns=["year"])
        print("\n예시 (현진소재 2018-12-19):")
        ex = events[events["drop_id"] == "053660_20181219"]
        if len(ex):
            for k in ("decision_time", "news_window_start", "news_window_end", "n_base_start", "p_base_start"):
                print(f"  {k}: {ex[k].iloc[0]}")

    renamed = names[names.duplicated("stock_code", keep=False)]
    if len(renamed):
        print("\n사명이 바뀐 종목 (뉴스는 그 당시 이름으로 검색):")
        for _, r in renamed.iterrows():
            print(f"  {r['stock_code']} {r['company_name']}: {r['from']} ~ {r['to']}")
    print(f"\nσ가 모자라 판단하지 못한 -10% 이하 하락일: {len(unjudged)}일")
    if len(unjudged):
        print(unjudged.head(10).to_string(index=False))

    OUTPUT_DIR.mkdir(exist_ok=True)
    names.to_csv(OUTPUT_DIR / "stock_names.csv", index=False, encoding="utf-8-sig")
    events.to_csv(OUTPUT_DIR / "drop_events.csv", index=False, encoding="utf-8-sig")
    sessions.to_csv(OUTPUT_DIR / "drop_sessions.csv", index=False, encoding="utf-8-sig")
    candidates.to_csv(OUTPUT_DIR / "drop_candidates.csv", index=False, encoding="utf-8-sig")
    print("\n저장 완료:", OUTPUT_DIR)
