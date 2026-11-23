# 뉴스 수집 경로별 원본 비교: RSS · 네이버 검색 API · 지금 크롤링

급락 모델(sudden)의 뉴스 근거를 어디서 받느냐에 따라 **실제로 손에 들어오는 원본이 어떻게 다른지** 정리했다.
수치는 2026-11-23에 이 저장소의 캐시와 공개 RSS 한 건을 직접 열어 잰 값이다.
기사 문장은 저작권 때문에 싣지 않고 `…`나 `<기사 제목>`처럼 자리표시로 바꿨다.

## 한눈에

| | RSS (언론사 피드) | 네이버 검색 API | 지금 방식: 검색 페이지 크롤링 + 기사 본문 |
|---|---|---|---|
| 받는 것 | 언론사의 최근 기사 목록 | 검색어에 맞는 기사 목록 | 검색 목록 → 기사 페이지를 열어 **본문** |
| 원본 형식 | XML | JSON | 페이지 HTML 그대로 (`.txt`) + 추출한 레코드 |
| 제목·링크·게시 시각 | 있음 | 있음 | 있음 (+ 수정 시각, 언론사) |
| 본문 | **없음**: 요약 약 190자에서 잘림 | **없음**: 요약 약 120자 | **전체 본문** (중앙값 약 720~750자) |
| 종목별 검색 | 안 됨 (분야별 피드) | 됨 (검색어) | 됨 (검색어) |
| 날짜 지정 | 안 됨 | **안 됨** (최신순 1,000건까지) | 됨 (날짜별 검색) |
| 과거 기사 | 최근 몇 시간치 | 종목당 최근 1,000건 | 2023년 등 과거도 가능 |
| 공식 여부 | 공개 피드 (이용 조건은 언론사별) | 공식 API, 키 필요, 하루 25,000회 | 웹 페이지 크롤링 (요청 간격 6초, robots.txt 준수) |
| 판정(ACH)에 바로 쓸 수 있나 | 아니오: 본문을 따로 받아야 함 | 아니오: 본문을 따로 받아야 함 | 예 |

**결론:** RSS와 API는 둘 다 **"어떤 기사가 있는지" 목록만** 준다. 판정은 본문 문장을 인용해 근거를 세우므로,
어느 경로든 결국 기사 링크를 열어 본문을 받아야 한다. 차이는 **목록을 어떻게 찾느냐**뿐이다.

---

## 1. RSS

연합뉴스 경제 피드(`https://www.yna.co.kr/rss/economy.xml`)를 2026-11-23 14시에 한 번 받은 결과.

```xml
<rss version="2.0" ...>
  <channel>
    <title>연합뉴스 경제 최신기사</title>
    <copyright>저작권자(c) 연합뉴스, 무단 전재-재배포, AI 학습 및 활용 금지</copyright>
    <lastBuildDate>Mon, 23 Nov 2026 14:07:00 +0900</lastBuildDate>
    <item>
      <title><기사 제목></title>
      <link>https://www.yna.co.kr/view/AKR2026112310…</link>
      <guid isPermaLink="true">https://www.yna.co.kr/view/AKR2026112310…</guid>
      <pubDate>Mon, 23 Nov 2026 14:07:00 +0900</pubDate>
      <dc:creator><기자 이름></dc:creator>
      <description>(창원=연합뉴스) <기자> 기자 = … 발표했다고...</description>
      <media:content url="https://img.yna.co.kr/…_01_i_P2.jpg" type="image/jpeg"/>
    </item>
    ... (120건)
  </channel>
</rss>
```

잰 값:
- 피드 하나에 **120건**, 기간은 **06:34 ~ 14:07 (약 7시간)**. 과거 기사는 없다.
- `description`은 **중앙값 189자, 최대 213자**에서 `...`로 잘린다. 본문이 아니다.
- 분야(경제) 전체가 섞여 나온다. 주식과 무관한 기사도 있고, 종목으로 거를 수단이 없다.
- 피드에 **"AI 학습 및 활용 금지"** 문구가 있다. 쓰려면 언론사별 이용 조건을 확인해야 한다.

쓸 수 있는 곳: 실시간으로 "오늘 새 기사가 나왔는지" 빠르게 감지하는 용도 정도. 과거 사건 검증에는 못 쓴다.

---

## 2. 네이버 검색 API (뉴스)

NAVER API HUB 뉴스 검색(`/search/v1/news`). 코드는 `models/sudden/news_api.py`, 운영 계획은 `docs/sudden_live_api.md`.
요청은 `query=<종목명>&display=100&start=1..1000&sort=date`.

응답 한 건 (API 원본 JSON 모양):

```json
{
  "lastBuildDate": "Mon, 23 Nov 2026 14:10:00 +0900",
  "total": 12345,
  "start": 1,
  "display": 100,
  "items": [
    {
      "title": "<b>종목명</b>, <기사 제목>",
      "originallink": "https://<언론사 도메인>/news/articleView.html?idxno=…",
      "link": "https://n.news.naver.com/mnews/article/0xx/00000xxxxx",
      "description": "… <b>종목명</b> … 요약 문장 …",
      "pubDate": "Mon, 23 Nov 2026 13:55:00 +0900"
    }
  ]
}
```

우리가 DB(`api.sqlite3`의 `items` 테이블)에 쌓는 칸: `link, stock_code, query, title, originallink, description, pub_at, fetched_at`.

잰 값 (`cache/naver_live_api/api.sqlite3`: 실시간 그림자 실행으로 쌓인 38종목, 37,979건):
- `description`은 **중앙값 122자, 최대 200자**. 검색어 주변을 잘라 낸 요약이고 본문이 아니다.
- **날짜를 지정할 수 없고 최신순 1,000건까지만** 준다. 종목·검색어마다 1,000건이 덮는 기간은
  **최소 22일, 중앙값 534일**. 기사가 많은 대형주는 한 달도 못 거슬러 간다.
- `link`가 네이버 뉴스(n.news.naver.com)인 건 **19%** (7,127건). 나머지는 네이버 링크 없이 언론사 원문 링크만 있다.
- 제목·요약에 `<b>` 강조 태그와 HTML 엔티티가 섞여 있어 정리가 필요하다.

쓸 수 있는 곳: **매일 목록을 쌓아 가는** 실시간 운영(크롤링의 "검색" 단계 대체). 본문은 여전히
`naver_crawl.py --no-search`로 기사 페이지를 열어 받는다. 2023년 사건 같은 과거 검증에는 1,000건 한도 때문에 못 쓴다.

---

## 3. 지금 방식: 검색 페이지 크롤링 + 기사 본문

`models/sudden/naver_crawl.py`. 사건마다 날짜 구간을 정해 네이버 뉴스 검색 결과 페이지를 날짜별로 받고(최대 10쪽),
**제목에 종목명이 있는 기사만** 기사 페이지를 열어 본문을 뽑는다. 네이버에 없는 기사는 언론사 원문을 받는다(robots.txt가 막으면 제외).
저장은 `crawl.sqlite3`(레코드) + 페이지 HTML 원본(`<sha256>.txt`). 모두 git에서 빠지는 `models/sudden/cache/`에만 둔다.

### 3-1. 원본 HTML (`raw_file`)
페이지를 받은 그대로 저장한다. 광고·스크립트·메뉴까지 포함이라 크다.

| 종류 | 예시 크기 | 첫 부분 |
|---|---|---|
| 검색 결과 페이지 | 약 238KB | `<meta property="og:title" content="&quot;<종목명>&quot; : 네이버 뉴스검색"/>` … |
| 네이버 기사 페이지 | 약 298KB | `<html lang="ko" data-useragent="investment-evidence-research/0.1">` … |
| 언론사 원문 페이지 | 약 57KB | 언론사마다 구조가 다름 |

### 3-2. 검색 레코드 (`searches` 테이블)
```json
{
  "stock_code": "023590",
  "date": "2022-12-27",
  "query": "<종목명>",
  "search_query": "\"<종목명>\"",
  "priority": 1,
  "drop_ids": ["…"],
  "collected_at": "2026-11-22T21:…",
  "status": "complete",
  "results": [
    {"title": "<기사 제목>", "original_url": "https://<언론사>/…", "naver_url": "https://n.news.naver.com/mnews/article/…"}
  ],
  "raw_files": ["models\\sudden\\cache\\naver_oos60\\<sha256>.txt"],
  "reason": null
}
```
→ 여기까지는 RSS·API와 같은 **목록** 수준이다(요약 문장도 쓰지 않는다).

### 3-3. 기사 레코드 (`articles` 테이블)
```json
{
  "url": "https://n.news.naver.com/mnews/article/659/0000008057",
  "collected_at": "2026-11-22T21:…",
  "title": "<기사 제목>",
  "publisher": "<언론사>",
  "published_at": "2022-12-28T…+09:00",
  "modified_at": "2022-12-28T…+09:00",
  "time_unknown": false,
  "body": "<기사 본문 전체>",
  "raw_file": "models\\sudden\\cache\\naver_oos60\\<sha256>.txt",
  "status": "complete"
}
```
→ **이 `body`가 RSS·API에는 없는 부분**이다. 판정(ACH)은 여기서 문장을 인용한다.
수정 시각(`modified_at`)도 받아서, 급락 판단 시점 뒤에 고쳐진 기사는 `modified_after_decision`으로 따로 표시한다.

잰 값 (60종목 재검증 수집, 2026-11-22 첫날 435번 검색 기준):
- 본문을 받은 기사: 네이버 213건(본문 중앙값 **717자**), 언론사 원문 605건(중앙값 **753자**).
- 가장 짧은 본문은 62~78자 (사진 기사·한 줄 속보).
- 요청 간격 6초, 검색 한 번에 기사 여러 건을 열어 **검색 1번당 평균 약 15초**. 그래서 느리다.

---

## 어느 경로를 어디에 쓰나

| 용도 | 목록 찾기 | 본문 |
|---|---|---|
| 과거 사건 검증 (백테스트, 60종목 재검증) | 검색 페이지 크롤링 (날짜 지정이 필요) | 기사 페이지 크롤링 |
| 매일 실시간 판정 (지금) | 검색 페이지 크롤링 | 기사 페이지 크롤링 |
| 매일 실시간 판정 (전환 계획, `docs/sudden_live_api.md`) | **네이버 검색 API** (매일 쌓기) | 기사 페이지 크롤링 |
| 새 기사 빠른 감지 (선택) | RSS | 기사 페이지 크롤링 |

어느 경로든 **본문은 기사 페이지에서 받는다.** RSS·API로 바꿔도 본문 수집과 그 속도·차단 위험은 남는다.
