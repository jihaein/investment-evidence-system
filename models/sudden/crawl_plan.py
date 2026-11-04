"""
crawl_plan.py — 급락 모델: 크롤링할 날짜 목록 만들기

[what]
events.py가 만든 사건 표(drop_events.csv)와 이름 표(stock_names.csv)를 읽어서,
"어느 종목을 어느 날짜에 어떤 이름으로 검색할지"를 한 줄씩 적은 목록을 만든다.
크롤러는 이 목록을 위에서부터 읽어 하루 단위·날짜순으로 검색하면 된다.

[우선순위] (시간이 부족하면 1부터 모음)
    1 event_window  사건의 뉴스 창: D-3일 ~ D일 (판단에 직접 쓰는 기사)
    2 p_base        파급력 P의 기준 기간: 뉴스 창 시작 전 90일
    3 n_base        신규성 N의 기준 기간: 뉴스 창 시작 전 180일 중 나머지
    한 날짜가 여러 사건에 걸리면 가장 높은 우선순위(작은 숫자)를 쓴다

[검색어]
    query      그날 쓰이던 회사 이름 (예: 050540은 2017-04-12까지 '엠피씨', 그 뒤 '한국코퍼레이션')
    query_alt  사명 변경일 앞뒤 60일에는 다른 이름도 같이 검색한다 (기사가 옛 이름·새 이름을 섞어 쓴다)
    약칭·영문명·주요 종속회사는 search_terms.csv에 팀이 직접 채운다 (이미 있으면 덮어쓰지 않는다)

[시간 규칙] 이 목록은 "어느 날짜를 검색할지"만 정한다. 기사를 사건에 쓸지는 기사의 분 단위 입력 시각으로
    drop_events.csv의 news_window_start(초과) ~ news_window_end(이하)를 다시 확인해서 정한다.
    그래서 D-3일은 하루 전체를 검색하고, 15:30 이전 기사는 나중에 걸러진다.
    크롤러는 기사마다 최초 입력 시각, 수정 시각, 수집 시각을 따로 저장해야 한다.

[실행] (먼저 python models/sudden/events.py 를 돌려 사건 표를 만든다)
    python models/sudden/crawl_plan.py
    python models/sudden/crawl_plan.py --max-priority 1     # 사건 창만

[결과] models/sudden/output/
    crawl_plan.csv     종목 × 날짜 목록 (stock_code, date, weekday, query, query_alt, priority, priority_name, drop_ids)
    search_terms.csv   종목별 검색어 표 (팀이 약칭·영문명·종속회사·제외 접두어를 채운다)
"""

import argparse
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent                       # models/sudden
OUTPUT_DIR = HERE / "output"
EVENTS_PATH = OUTPUT_DIR / "drop_events.csv"
NAMES_PATH = OUTPUT_DIR / "stock_names.csv"
PLAN_PATH = OUTPUT_DIR / "crawl_plan.csv"
TERMS_PATH = OUTPUT_DIR / "search_terms.csv"

RENAME_OVERLAP_DAYS = 60                                     # 사명 변경일 앞뒤 이 기간에는 두 이름을 같이 검색
PRIORITY_NAMES = {1: "event_window", 2: "p_base", 3: "n_base"}
WEEKDAYS = "월화수목금토일"


def to_day(value):
    return pd.Timestamp(str(value)[:10])                     # "2018-12-14T15:30:00+09:00" → 2018-12-14


def load(events_path=EVENTS_PATH, names_path=NAMES_PATH):
    events = pd.read_csv(events_path, dtype={"stock_code": str}, encoding="utf-8-sig")
    names = pd.read_csv(names_path, dtype={"stock_code": str}, encoding="utf-8-sig")
    names["from"], names["to"] = pd.to_datetime(names["from"]), pd.to_datetime(names["to"])
    return events, names.sort_values(["stock_code", "from"]).reset_index(drop=True)


# 그날 쓰이던 이름: 시작일이 그날 이전인 이름 중 가장 최근 것 (그 전이면 첫 이름)
def name_on(name_rows, d):
    valid = name_rows[name_rows["from"] <= d]
    return (valid.iloc[-1] if len(valid) else name_rows.iloc[0])["company_name"]


# 사명 변경일 앞뒤 RENAME_OVERLAP_DAYS일 안이면 다른 이름도 같이 검색
def alt_names(name_rows, d):
    current, alts = name_on(name_rows, d), set()
    for i in range(1, len(name_rows)):
        if abs((d - name_rows.iloc[i]["from"]).days) <= RENAME_OVERLAP_DAYS:
            alts |= {name_rows.iloc[i - 1]["company_name"], name_rows.iloc[i]["company_name"]}
    return sorted(alts - {current})


def build_plan(events, names, max_priority=3):
    days = {}
    for _, e in events.iterrows():
        D = to_day(e["D"])
        win = to_day(e["news_window_start"]) if pd.notna(e["news_window_start"]) else D
        p_base = to_day(e["p_base_start"]) if pd.notna(e["p_base_start"]) else win
        n_base = to_day(e["n_base_start"]) if pd.notna(e["n_base_start"]) else win
        for d in pd.date_range(n_base, D):
            pri = 1 if d >= win else (2 if d >= p_base else 3)
            slot = days.setdefault((e["stock_code"], d), {"priority": pri, "drop_ids": set()})
            slot["priority"] = min(slot["priority"], pri)
            if pri == 1:
                slot["drop_ids"].add(e["drop_id"])
    by_code = {c: g.reset_index(drop=True) for c, g in names.groupby("stock_code")}
    rows = []
    for (code, d), slot in sorted(days.items()):
        if slot["priority"] > max_priority:
            continue
        name_rows = by_code.get(code)
        rows.append({
            "stock_code": code, "date": d.strftime("%Y-%m-%d"), "weekday": WEEKDAYS[d.weekday()],
            "query": name_on(name_rows, d) if name_rows is not None else "",
            "query_alt": "|".join(alt_names(name_rows, d)) if name_rows is not None else "",
            "priority": slot["priority"], "priority_name": PRIORITY_NAMES[slot["priority"]],
            "drop_ids": "|".join(sorted(slot["drop_ids"])),
        })
    return pd.DataFrame(rows, columns=["stock_code", "date", "weekday", "query", "query_alt",
                                       "priority", "priority_name", "drop_ids"])


# 팀이 채울 검색어 표. 이미 있으면 사람이 고친 내용을 지키기 위해 덮어쓰지 않는다
def write_search_terms(names, path=TERMS_PATH):
    if Path(path).exists():
        return False
    t = names.copy()
    t["from"], t["to"] = t["from"].dt.strftime("%Y-%m-%d"), t["to"].dt.strftime("%Y-%m-%d")
    for col in ("약칭", "영문명", "주요_종속회사", "제외_접두어"):
        t[col] = ""
    t.to_csv(path, index=False, encoding="utf-8-sig")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="크롤링할 날짜 목록 만들기")
    ap.add_argument("--max-priority", type=int, default=3, choices=[1, 2, 3],
                    help="1: 사건 창만, 2: + 파급력 기준, 3: 전체 (기본)")
    args = ap.parse_args()
    if not EVENTS_PATH.exists():
        raise SystemExit("drop_events.csv가 없습니다. 먼저 python models/sudden/events.py 를 실행하세요.")

    events, names = load()
    plan = build_plan(events, names, args.max_priority)
    OUTPUT_DIR.mkdir(exist_ok=True)
    plan.to_csv(PLAN_PATH, index=False, encoding="utf-8-sig")
    wrote_terms = write_search_terms(names)

    adjusted = events["market_adjusted"].astype(str).str.lower().eq("true").all()
    print(f"[사건] {len(events)}건 ({'시장 조정' if adjusted else '시장 조정 없음 (근사)'})")
    print(f"[목록] 종목 × 날짜 {len(plan)}줄, 기간 {plan['date'].min()} ~ {plan['date'].max()}")
    print("우선순위별:", {f"{p} {PRIORITY_NAMES[p]}": int(n) for p, n in plan["priority"].value_counts().sort_index().items()})
    per = plan.groupby("stock_code").agg(days=("date", "size"), window_days=("priority", lambda s: int((s == 1).sum())),
                                         names=("query", lambda s: "/".join(dict.fromkeys(s))))
    print("\n종목별 (전체 날짜 / 사건 창 날짜):")
    for code, r in per.iterrows():
        print(f"  {code} {r['names']}: {r['days']} / {r['window_days']}")
    alt = plan[plan["query_alt"] != ""]
    print(f"\n두 이름을 같이 검색하는 날: {len(alt)}줄", alt.groupby("stock_code")["query_alt"].first().to_dict() if len(alt) else "")
    searches = len(plan) + int(alt["query_alt"].str.count(r"\|").add(1).sum()) if len(alt) else len(plan)
    print(f"검색 횟수 (페이지 넘김 제외): 약 {searches}번")
    print("\n저장 완료:", PLAN_PATH)
    print("검색어 표:", TERMS_PATH, "(새로 만듦)" if wrote_terms else "(이미 있어서 그대로 둠)")
