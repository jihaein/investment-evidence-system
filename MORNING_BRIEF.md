# ☀️ 아침 브리핑 — DB 마무리 작업 (밤새 준비해둔 것)

안녕 지해! 자는 동안 **실행은 못 했지만**(네 맥/Supabase 자격증명이 필요해서), 네가 바로 돌릴 수 있게
**코드·설정·문서·계획을 전부 준비해서 레포에 넣어뒀어.** 아래 순서대로 하면 돼.

## ✅ 지금 상태
- `prices`(182,823) · `stocks`(120) · `signals`(45,360) 적재 완료, API `/signal` 동작 확인됨.
- **좋은 소식**: 원천 데이터가 **207MB(463개 파일)** → Supabase 무료 Storage(1GB)에 그냥 들어감. R2 불필요.

## 📦 밤새 새로 넣어둔 파일
- `data_pipeline/README.md` — 파이프라인·DB 전체 문서 (**팀에 가져가서 보여주기 좋음**)
- `data_pipeline/supabase_setup.sql` — RLS 읽기 정책 + 인덱스
- `data_pipeline/archive_snapshots.py` — 원천 트리를 zip 1개로 묶는 스크립트
- `MORNING_BRIEF.md` — 이 파일

---

## 🏃 아침 실행 순서

### 1) RLS·인덱스 (5분, 안전)
Supabase → SQL Editor → `data_pipeline/supabase_setup.sql` 내용 붙여넣고 Run.
→ 끝에 정책 목록 나오면 성공. (앱이 나중에 Supabase 직접 조회할 때 필요)

### 2) 원천 데이터 Storage 아카이브 (Drive 의존 끊기)
```bash
cd ~/Developer/investment-evidence-system
.venv/bin/python data_pipeline/archive_snapshots.py
```
→ `data_pipeline/_archive/seoseohi_rawdata_2020-2026.zip` 생성(~100MB).
→ Supabase → Storage → New bucket(`rawdata`, private) → 그 zip 업로드.
⚠️ zip 은 git 에 커밋하지 말 것(용량). `.gitignore` 에 `data_pipeline/_archive/` 한 줄 추가해줘.

### 3) 증분 적재(upsert)로 전환 — **같이 하면 좋음 (내가 테스트 도와줄게)**
지금 `load_prices`·`compute_signals`는 "싹 지우고 재적재". 아래로 바꾸면 "새 것만 추가/갱신".
⚠️ **중요 함정**: `load_prices`가 `stocks`의 KSIC 보강(이름·업종·DART코드)을 **덮어쓰면 안 됨.**
그래서 stocks 는 `DO NOTHING`(없으면만 추가), prices 만 `DO UPDATE`.

**`load_prices.py`의 `load()` 함수 교체안:**
```python
def load(stocks, prices, db_url):
    engine = create_engine(db_url)
    with engine.begin() as con:
        # stocks: 없는 종목만 기본정보 추가 (KSIC 보강은 건드리지 않음)
        stocks.to_sql("_stg_stocks", con, if_exists="replace", index=False)
        con.execute(text("""
            insert into stocks (stock_code, stock_name, market)
            select stock_code, stock_name, market from _stg_stocks
            on conflict (stock_code) do nothing
        """))
        con.execute(text("drop table _stg_stocks"))
        # prices: 같은 (종목,날짜)면 갱신, 없으면 추가
        prices.to_sql("_stg_prices", con, if_exists="replace", index=False)
        con.execute(text("""
            insert into prices (stock_code, bas_dd, market, mktcap, list_shrs, close_prc, quality_status)
            select stock_code, bas_dd, market, mktcap, list_shrs, close_prc, quality_status from _stg_prices
            on conflict (stock_code, bas_dd) do update set
                market=excluded.market, mktcap=excluded.mktcap, list_shrs=excluded.list_shrs,
                close_prc=excluded.close_prc, quality_status=excluded.quality_status
        """))
        con.execute(text("drop table _stg_prices"))
    with engine.connect() as con:
        ns  = con.execute(text("select count(*) from stocks")).scalar()
        npx = con.execute(text("select count(*) from prices")).scalar()
    return ns, npx
```
(⚠️ 그러면 `truncate ... signals` 가 사라지니, signals 재계산이 필요하면 compute_signals 를 따로 돌려.)

**`compute_signals.py`의 적재부 교체안** (truncate → upsert):
```python
    df.to_pickle("data_pipeline/_signals_cache.pkl")
    with engine.begin() as con:
        df.to_sql("_stg_signals", con, if_exists="replace", index=False)
        con.execute(text("""
            insert into signals (stock_code, baseline_date, eval_date, dupont_score,
                                 ohlson_score, fscore, sum_total, verdict_count, verdict_sum)
            select stock_code, baseline_date, eval_date, dupont_score,
                   ohlson_score, fscore, sum_total, verdict_count, verdict_sum from _stg_signals
            on conflict (stock_code, baseline_date, eval_date) do update set
                dupont_score=excluded.dupont_score, ohlson_score=excluded.ohlson_score,
                fscore=excluded.fscore, sum_total=excluded.sum_total,
                verdict_count=excluded.verdict_count, verdict_sum=excluded.verdict_sum
        """))
        con.execute(text("drop table _stg_signals"))
```
→ 둘 다 **먼저 스모크 테스트**(돌려보고 행 수 그대로인지 확인) 후 신뢰할 것. 내가 테스트 같이 봐줄게.

### 4) 커밋
```bash
git add data_pipeline/ backend/api.py MORNING_BRIEF.md
git commit -m "DB 파이프라인 문서화 + RLS/인덱스 + Storage 아카이브 스크립트"
git push
```
(단, `_archive/` zip 은 빼고 — .gitignore 에 추가했으면 자동 제외됨.)

---

## 🤔 팀과 정할 것 (게임서버 일정에 큰 영향)
- **멀티플레이가 '실시간 동시접속'인지 '비동기(섬 방문·리더보드)'인지** 먼저 결정.
  - 비동기 = API 엔드포인트 몇 개 더 → 수일~2주
  - 실시간 = WebSocket/Colyseus 게임서버 구축 → 수주+
  이 결정 하나가 "일주일 vs 한 달+"을 가른다. 호스팅(배포) 자체는 몇 시간.

## 다음 번에 나랑 할 것 (추천 순서)
1. 위 3번(upsert) 스모크 테스트 + 완성
2. `/signal` 외 엔드포인트(내 포트폴리오 목록 + 각 종목 현재 판정) + 상장폐지 컷오프
3. 백엔드(FastAPI) 클라우드 배포(Render/Railway) — 반나절

잘 자고, 일어나서 1번부터 하면 돼. 수고했어 🙌
