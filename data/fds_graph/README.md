> 이 폴더는 FDS 공통 인과그래프 패키지(graph_version g1.1)의 `graph.json`과 `edge_cards/`를 그대로 옮긴 것이다. 아래는 원본 패키지의 설명서이며, 여기 나오는 `FDS_value_causal_graph.html`, `build_html.py`, `viewer.html`, `verify_v4.py`는 이 저장소에 넣지 않았다. Neo4j 적재는 `backend/graph/`를 본다.

> **검토 상태 변경 (2026-11-11):** 아래 원본 설명서는 "모든 카드는 draft"라고 적고 있지만, 팀 결정으로 g1.1의 카드 641개를 모두 `approved`로 바꿨다. 바꾼 칸은 각 카드의 `review_status` 하나뿐이다.

# FDS 공통 인과그래프 — 설계서 v4 구조 복원

기준: 사용자가 제공한 `FDS_rebuttal_model_design_v4_supplemented(1).md`의 3.2~3.6절. 이번 작업 범위는 S1 공통 그래프, S2 화살표 카드, 그리고 이 두 자료를 보여주는 HTML이다. 기업 논증·투자자 논증·태깅 엔진을 추가로 구현하지 않았다.

## 파일 구조

- `graph.json`: graph_version, nodes, edges. 노드와 연결을 정의한다.
- `edge_cards/*.json`: 각 관계의 설명·성립 조건·깨지는 조건·출처. 파일 안의 edge_id로 graph.json과 연결한다.
- `FDS_value_causal_graph.html`: 위 JSON을 그대로 내장한 탐색 화면.
- `build_html.py`, `viewer.html`: JSON 수정 후 화면을 재생성하는 코드와 템플릿.
- `verify_v4.py`: 필드 집합·타입·허용값·참조·부호·3.5 조건 계산·HTML 데이터 일치 검사.

별도 nodes.json을 만들지 않았다. edge_cards.json 일괄 파일도 패키지에는 넣지 않고 설계서의 개별 카드 폴더 구조를 따른다. HTML의 cardsData는 이 개별 카드들을 불러오기 위한 화면 내부 묶음이며, 카드마다의 필드는 파일과 동일하다.

## 실제 필드

노드 — 8개:
`node_id, layer, label_ko, synonyms_ko, observable, source_variable, unit, can_be_claim`

관계 — 5개:
`edge_id, cause, effect, sign, edge_type`

카드 — 8개:
`edge_id, sign, empirical_sign, mechanism, conditions, counterconditions, source, review_status`

깨지는 조건 — 4개:
`cc_id, text, check_node, check_direction`

source는 설계서 예시의 `title, author, source_class`를 사용한다. 연구의 URL과 원자료 행 ID는 출처 title 문자열에 함께 적었다. 원문 연구 맥락은 conditions에 담았다. 새 출처 필드를 만들지 않았다.

## 복원한 사항

- 노드의 label, label_en, aliases, core, review_status, mapping_status, domains, component_id, degree, layout을 제거했다.
- 관계의 empirical_sign, review_status, runtime_enabled, sign_counts, row_ids, paper_count, domains, core 등을 제거했다. 실증 부호와 검수 상태는 카드에만 둔다.
- 카드의 competing_parent_candidates, source_rows, runtime_enabled를 제거했다.
- sign은 + 또는 -만 허용한다. graph와 해당 카드의 sign은 같다.
- 다른 부모 S의 조건은 counterconditions에 저장하고, 설계서 3.5의 명시된 식대로 check_direction = -sign(S→B)로 계산한다. 다른 부모의 부호가 +이면 조건 방향은 -, -이면 +다.
- 반대/None 연구 맥락이 유지된 관계에 있으면 check_node=null인 맥락 조건으로 남긴다.
- L3→L4에는 이미 주가에 반영된 경우와 시장 위험 프리미엄 변화 조건을 넣는다.
- 모든 카드는 draft다. 설계서대로 approved만 실제 런타임에서 사용한다. 별도 실행 플래그는 없다.
- 좌표·이웃 수·화면 선택·검색 상태는 HTML의 내부 상태로 계산한다. graph.json에 저장하지 않는다.

## 숫자와 남은 검토

수정 그래프: **563개 노드, 641개 관계, 641개 카드**.

기존 사용 명제 659개 중 538개가 수정 그래프에 남는다. 나머지 121개는 아래 표에서 원자료 행별로 확인할 수 있다. 원자료 전체 10,490개 중 기존 버전에서 제외했던 9,831개를 이번 구조 복원 작업에서 다시 의미 분류한 것은 아니다. 따라서 이번 수정이 새로운 전수 의미 검토 완료를 뜻하지 않는다.

None/mixed/context_dependent만으로 이론 sign을 정할 수 없는 관계는 임의로 +/−를 넣지 않았다. 주가 움직임을 원인 근거로 쓰는 관계도 설계서 1.5에 따라 활성 그래프에서 제외했다. 그런 관계 제거 뒤 가치까지의 경로가 끊긴 관계도 아래에 명시했다. 그래프에 남긴 실증 관계의 sign은 단일 방향 실증 결과를 참고한 **검수 전 방향 가설**이며, 그 자체로 독립적인 이론 검증이나 사람 승인을 마친 것은 아니다. 실증과 이론의 부호를 별도 필드로 두고 모든 카드를 draft로 남긴 이유다.

실증 카드의 mechanism은 대상 변수별 경제적 경로를 제안한 LLM 초안이다. 논문에 그 메커니즘이 직접 증명되어 있다는 뜻은 아니다. 원문 검토로 보강하고 승인해야 한다.

## observable과 실제 서서히 모델 연결

실제 T1 변수 목록·코드가 제공되지 않았기 때문에 실제 데이터 연동이 완료됐다고 표시하지 않았다. 설계서에 관측 재무변수로 등장하는 다음 15개는 observable=true인 **설계 단계 초안**으로 되돌렸다.

revenue, cogs, capex, nwc_change, debt, cash, shares_outstanding, gross_margin, operating_margin, leverage, operating_profit, nopat, reinvestment, fcff, net_debt.

source_variable은 실제 서서히 모델 경로를 추측하지 않고 null로 남겼다. 관계에서 계산 가능한 gross_margin, operating_margin, reinvestment, fcff, net_debt에는 설계서가 허용한 산식 문자열을 넣었다. 나머지 노드는 현재 모델로 값을 확보한다고 확인하지 못했으므로 false 초안이다. **T1 목록을 받으면 true/false와 실제 경로를 대조·확정해야 한다.** false를 ‘값을 알 수 있는 변수인데 지금 회사 데이터만 비어 있음’으로 사용하는 로직은 넣지 않았다. 회사별 값의 부재는 설계서의 company_argument 노드 status에서 처리할 사항이다.

## HTML 사용

가치 중심 / 재무 연결 / 전체 그래프 / 노드 목록 / 관계 목록을 제공한다. 노드 클릭 시 8개 필드와 실제 JSON을, 관계 클릭 시 5개 필드와 연결된 카드 8개 필드를 보여준다. 이론 sign과 실증 empirical_sign은 따로 표시한다. 화면의 최단 경로는 탐색 보조이며 승인된 경제적 추론을 뜻하지 않는다.

JSON 수정 후 같은 폴더에서:

```bash
python build_html.py
python verify_v4.py
```

## 검증

필드의 정확한 집합, 값 타입·허용값, 고유 ID, 참조 무결성, 그래프와 카드 sign 일치, 모든 다른 부모의 3.5 조건 방향, HTML 내장 JSON과 파일 JSON의 완전 일치를 검사했다. 검색·목록·노드·카드·경로 코드는 모의 DOM에서 실행했다.

실제 브라우저 시각 검수는 완료하지 못했다. 로컬 Chromium 실행은 환경 제약으로 실패했고, 브라우저 도구도 file 프로토콜을 차단했다. 이 제한을 우회하지 않았다. 따라서 실제 화면 배치를 확인했다고 주장하지 않는다.

## 이번 수정에서 그래프에 넣지 않은 기존 사용 명제

| 원자료 행 | 원인 | 결과 | 원자료 부호 | 사유 |
|---|---|---|---|---|
| R00012 | Teen childbearing | Family income | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R00109 | Trade liberalization | Labor productivity | + | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R00701 | Trade liberalization | Plant productivity | + | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R02325 | Trade liberalization | Within-firm productivity | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R00214 | Hiring high-wage workers | Firm profitability | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R00239 | Employer provided training | Establishment productivity | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R00454 | Winning $15,000 per year | Labor supply | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R00498 | Corporate tax rate | Business investment | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R00565 | Cash flow ownership of controlling shareholder | Corporate valuation | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R00636 | Government ownership of banks | Capital accumulation | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R00711 | Stricter regulation of entry | Profitability of incumbent firms | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R00764 | Unemployment news (surprise) | Equity risk premium | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R00978 | Price increase absent cash-flow news | Institutional ownership | - | 설계서 1.5: 주가 움직임을 원인 근거로 쓰는 관계는 반박 그래프에 사용하지 않음 |
| R01133 | Product market competition | Innovation rate | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R01143 | Share of college graduates in city | Productivity of high-tech plants | + | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R01144 | Share of college graduates in city | Productivity of plants that often interact | + | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R01145 | Share of college graduates in city | Productivity of low-tech plants | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R01317 | Transitory shocks in wealth | Consumer spending | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R01412 | Large debt issues | Future interest rates | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R01467 | Financial intermediation | Per-capita GDP growth | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R01487 | Unanticipated monetary policy actions | Expected real interest rates | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R01542 | Raising capital when stock prices high | Leverage ratio | - | 설계서 1.5: 주가 움직임을 원인 근거로 쓰는 관계는 반박 그래프에 사용하지 않음 |
| R01551 | Large depreciation | Leverage | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R01581 | Bylaws-based staggered boards | Firm value (Tobin's Q) | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R01592 | Predicted increases in temperature and precipitation | Annual agricultural profits | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R01697 | Internationalization | Tobin's q | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R01745 | Income per capita | Democracy | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R01748 | Income per capita | Democracy | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R01747 | Average years of schooling | Democracy | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R01751 | Education | Democracy | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R02022 | Delicensing | Fixed capital | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R02048 | Firm-specific changes in equity risk premia | Firm investment (capital growth) | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R02157 | Charging positive online prices | Firm profits | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R02163 | Average bank liquidity decline | Firm aggregate borrowing | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R02276 | R&D by product market rivals | Firm total factor productivity | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R02295 | London listing | Firm valuation | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R02413 | Non-fundamental component of market valuation | Firm investment | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R02498 | Monetary policy shock | Inflation | + | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R10279 | Monetary policy shock | Inflation | - | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R02567 | Venture capital financing | Firm profitability prior to exit | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R02678 | Offering full-line forcing contracts | Distributor profits | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R02815 | Business Combination laws | Capital expenditures | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R03003 | Addition of a Fitch rating | Credit spreads | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R03027 | Federal tax revenue change | Real GDP | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R03075 | Bank health | Exports of firms with foreign affiliates | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R03199 | Stock return variance | Corporate default rate | + | 설계서 1.5: 주가 움직임을 원인 근거로 쓰는 관계는 반박 그래프에 사용하지 않음 |
| R03200 | Stock market return | Corporate default rate | - | 설계서 1.5: 주가 움직임을 원인 근거로 쓰는 관계는 반박 그래프에 사용하지 않음 |
| R03201 | Realized default rates | Credit spreads | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R03334 | Credit lines | Planned investment | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R03476 | Nitrate violations | Bottled water sales | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R03538 | Non-blood heir succession | Tobin's Q ratio | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R03886 | Starting career during recession | Return on assets | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R03982 | Investment subsidy program (RSA) | Total factor productivity | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R03999 | Corporate default crisis | GDP growth | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R04206 | Target acquisition excess returns | Firm R&D | + | 설계서 1.5: 주가 움직임을 원인 근거로 쓰는 관계는 반박 그래프에 사용하지 않음 |
| R04209 | Abolition of informality | Average firm profits | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R04253 | Any treatment (insurance or cash) | total farm profits | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R04341 | Tax-inclusive fuel price | Grey (energy-efficiency) innovation | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R04344 | R&D subsidies for clean technologies | Clean innovation | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R04552 | Supply-side financial shocks | Firm investment | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R04976 | Increase in collateral value | Firm survival (exit probability) | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R05086 | Economic uncertainty | Corporate leverage | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R05114 | Electricity shortages | Revenue productivity (TFPR) | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R05214 | Brand keyword paid search | Short-term sales | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R05215 | Non-brand paid search advertising | Total sales | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R05323 | Cash grant | Gross profit | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R05333 | Bank distress | Patenting by less finance-dependent firms | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R05430 | Oil and gas boom | Tradable manufacturing productivity | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R05530 | Additional H-1B visa | Firm patenting | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R05550 | Weekly POS deposit collection | Household consumption | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R05595 | Corporate behavioral frictions | Large credit spreads | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R05597 | Bond market illiquidity | Large credit spreads | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R05598 | Investors' over-estimation of default probabilities | High credit spreads | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R05642 | Confidence shock | Labor productivity and TFP | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R05643 | Confidence shock | Inflation | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R05672 | Giant oil discovery NPV | Private consumption | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R05719 | 2003 dividend tax cut | Corporate investment | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R06131 | Adding a management layer | Quantity-based productivity | + | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R06133 | Adding a management layer | Revenue-based productivity | - | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R06148 | Bonus depreciation | Investment by nontaxable firms | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R06497 | Regime changes in monetary policy | Short-term real interest rate | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R06498 | Regime changes in monetary policy | Real interest rate | - | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R06834 | Agency mortgage purchases | Unemployment rate | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R06917 | Price overvaluation | Physical capital investment | + | 설계서 1.5: 주가 움직임을 원인 근거로 쓰는 관계는 반박 그래프에 사용하지 않음 |
| R07201 | Private equity backing | Leverage ratio | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R07329 | Worker unionization | Firm profitability or survival | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R07463 | Stock price informativeness | Total Factor Productivity | + | 설계서 1.5: 주가 움직임을 원인 근거로 쓰는 관계는 반박 그래프에 사용하지 않음 |
| R07468 | Stock price informativeness | Return on assets | + | 설계서 1.5: 주가 움직임을 원인 근거로 쓰는 관계는 반박 그래프에 사용하지 않음 |
| R07647 | Harvest-time loan offer | Household consumption | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R07850 | Main Business Cycle shock | Total factor productivity (TFP) | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R07854 | Main Business Cycle shock | Inflation | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R08220 | Mandatory gender pay transparency | Firm profitability | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R08449 | Personality traits and demographic similarity | Initial productivity and learning rate | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R08584 | VC equity share | Startup value | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R08777 | Board-level codetermination | Firm survival | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R08778 | Board-level codetermination | Revenue | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R08779 | Board-level codetermination | Profitability | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R08820 | One-time cash transfers | Enterprise profits | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R08821 | One-time cash transfers | Firm fixed capital investment | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R08957 | Stock market decline | Household consumption | - | 설계서 1.5: 주가 움직임을 원인 근거로 쓰는 관계는 반박 그래프에 사용하지 않음 |
| R08964 | Bank equity crash (>=30%) | Real GDP | - | 설계서 1.5: 주가 움직임을 원인 근거로 쓰는 관계는 반박 그래프에 사용하지 않음 |
| R08965 | Panic during bank equity crash | Real GDP | - | 설계서 1.5: 주가 움직임을 원인 근거로 쓰는 관계는 반박 그래프에 사용하지 않음 |
| R09229 | Being undercapitalized | loan supply to risky firms | - | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R09230 | Being undercapitalized | lending to zombie firms | + | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R09613 | Local temperature increase | Fundamental productivity | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R09786 | Increase in bank size | Bank profitability and efficiency | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R10096 | Unilateral reductions in department input bias | Firm revenue | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R10124 | Exposure by anti-corruption audits | Firm exit probability | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R10133 | Disclosure regulation | Firm value | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R10150 | Investment flexibility | Capital expenditure growth | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R10218 | Increase in monetary market access | Manufacturing capital | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R10223 | Dividend tax increase | Capital accumulation | + | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R10227 | Dividend tax increase | Capital accumulation of equity-dependent firms | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R10258 | Mergers of network owners | Combined advertising revenues | mixed | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R10395 | Non-financial flood shock to parents | Affiliate sales | None | 설계서 3.3의 이론 sign(+/-) 초안을 확정할 근거 부족. None/mixed/context_dependent를 임의 부호로 변환하지 않음 |
| R00830 | Institutions of private property | Income per capita | + | 미확정 관계를 제거한 뒤 가치 목표로 이어지는 경로가 없음 |
| R00831 | European settlement | Institutions of private property | + | 미확정 관계를 제거한 뒤 가치 목표로 이어지는 경로가 없음 |
| R01301 | Judicial independence | Government ownership of banks | - | 미확정 관계를 제거한 뒤 가치 목표로 이어지는 경로가 없음 |
| R04339 | Perry Preschool program | Education | + | 미확정 관계를 제거한 뒤 가치 목표로 이어지는 경로가 없음 |
| R09664 | Opening bank branches | Income per capita | + | 미확정 관계를 제거한 뒤 가치 목표로 이어지는 경로가 없음 |
| R10281 | Slaughter of the bison | Income per capita | - | 미확정 관계를 제거한 뒤 가치 목표로 이어지는 경로가 없음 |
