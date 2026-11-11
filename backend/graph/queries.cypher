// FDS 인과그래프 예시 쿼리
// Neo4j Browser에서는 아래 :param 줄을 먼저 한 번 실행한다 (세션 동안 유지됨).
// 값을 바꾸려면 :param 을 고쳐서 다시 실행한다.
// g1.1 카드 641개는 팀 결정(2026-11-11)으로 모두 approved다. approved_only: true면 승인된 카드만 쓴다.
:param {approved_only: false, q: '자기자본비용', start: 'beta', claim_start: 'revenue', eid: 'beta->cost_of_equity', v: 'risk_free_rate'};

// 1. 개요: 레이어별 노드 수
MATCH (n:Variable) RETURN n.layer AS layer, count(*) AS nodes ORDER BY layer;

// 2. 한글 이름·동의어로 변수 찾기 (fulltext)
CALL db.index.fulltext.queryNodes('variable_label_ko', $q) YIELD node, score
RETURN node.node_id, node.label_ko, node.layer, score LIMIT 10;

// 3. 어떤 변수에서 주장 노드(:Claim)까지 가는 경로와 누적 부호
MATCH p = (s:Variable {node_id: $start})-[:CAUSES*1..6]->(c:Claim)
WHERE all(r IN relationships(p) WHERE NOT $approved_only OR r.review_status = 'approved')
RETURN c.label_ko AS claim,
       [n IN nodes(p) | n.label_ko] AS path,
       reduce(x = 1, r IN relationships(p) | x * CASE r.sign WHEN '+' THEN 1 ELSE -1 END) AS net_sign
ORDER BY length(p) LIMIT 25;

// 4. 관계 하나의 카드 전체: 메커니즘·조건·출처·깨지는 조건
MATCH (c:EdgeCard {edge_id: $eid})-[:CITES]->(src:Source)
OPTIONAL MATCH (c)-[:HAS_COUNTER]->(cc)
OPTIONAL MATCH (cc)-[:CHECKS]->(v)
RETURN c.mechanism, c.conditions, src.title, src.source_class,
       collect({text: cc.text, check: v.label_ko, direction: cc.check_direction}) AS counterconditions;

// 5. 주장 "X가 오르면 주주가치가 오른다"를 반박할 때 확인할 변수
//    경로 위 각 관계의 깨지는 조건을 모아 확인 대상 변수별로 묶음
//    (revenue → equity_value 최단 경로가 6단계라서 한도를 7로 둔다)
MATCH p = (s:Variable {node_id: $claim_start})-[:CAUSES*1..7]->(:Variable {node_id: 'equity_value'})
UNWIND relationships(p) AS r
MATCH (c:EdgeCard {edge_id: r.edge_id})-[:HAS_COUNTER]->(cc)-[:CHECKS]->(v)
RETURN v.label_ko AS check_variable, cc.check_direction AS direction,
       count(DISTINCT r.edge_id) AS edges_affected, collect(DISTINCT cc.text)[0..3] AS examples
ORDER BY edges_affected DESC LIMIT 20;

// 6. 이 변수가 반박 근거로 쓰이는 곳 (역방향)
MATCH (cc:CounterCondition)-[:CHECKS]->(:Variable {node_id: $v}), (c:EdgeCard)-[:HAS_COUNTER]->(cc)
RETURN c.edge_id, cc.check_direction, cc.text LIMIT 50;

// 7. 이론 sign과 실증 empirical_sign이 다른 관계
//    (g1.1 기준 0건: 실증 관계 528개는 모두 sign = empirical_sign. 카드 검토로 sign이 바뀌면 여기 잡힌다)
MATCH (a)-[r:CAUSES]->(b)
WHERE r.empirical_sign IN ['+', '-'] AND r.empirical_sign <> r.sign
RETURN a.label_ko, b.label_ko, r.sign, r.empirical_sign;

// 8. 같은 출처를 근거로 한 관계 묶음
MATCH (src:Source)<-[:CITES]-(c:EdgeCard)
WITH src, collect(c.edge_id) AS edges WHERE size(edges) > 1
RETURN src.title, src.source_class, size(edges) AS n, edges ORDER BY n DESC LIMIT 20;

// 9. 시각화용: 주장 노드 주변 2단계
MATCH p = (:Variable)-[:CAUSES*1..2]->(:Claim {node_id: 'equity_value'}) RETURN p LIMIT 300;


// ───────── HTML 뷰어(FDS_value_causal_graph.html)의 다섯 화면과 같은 결과 ─────────
// 그래프 화면(H1~H3)은 Graph 탭, 목록 화면(H4~H5)은 Table 탭으로 본다.

// H1. 가치 중심: 영업자산 가치·주주가치·기대 FCFF와 바로 연결된 노드 (20 노드 · 19 관계)
MATCH (r:Variable) WHERE r.node_id IN ['firm_value', 'equity_value', 'expected_fcff']
MATCH (r)-[:CAUSES]-(n)
WITH collect(DISTINCT r) + collect(DISTINCT n) AS ns
MATCH (a)-[e:CAUSES]->(b) WHERE a IN ns AND b IN ns
RETURN a, e, b;

// H2. 재무 연결: 설계서 이름이 붙은 노드(n_ 로 시작하지 않는 것)끼리의 관계 (105 노드 · 121 관계)
MATCH (a:Variable)-[e:CAUSES]->(b:Variable)
WHERE NOT a.node_id STARTS WITH 'n_' AND NOT b.node_id STARTS WITH 'n_'
RETURN a, e, b;

// H3. 전체 그래프 (563 노드 · 641 관계)
//     Browser는 기본으로 노드 300개까지만 그린다. 설정(톱니바퀴) → Initial Node Display 를 1000으로 올린다.
MATCH (a:Variable)-[e:CAUSES]->(b:Variable)
RETURN a, e, b;

// H4. 노드 목록 (563행)
MATCH (n:Variable)
OPTIONAL MATCH (n)-[e:CAUSES]-()
RETURN n.node_id, n.layer, n.label_ko, n.synonyms_ko, n.observable, n.source_variable, n.unit, n.can_be_claim,
       count(e) AS degree
ORDER BY n.layer DESC, degree DESC;

// H5. 관계 목록 + 카드 (641행)
MATCH (a:Variable)-[e:CAUSES]->(b:Variable), (c:EdgeCard {edge_id: e.edge_id})-[:CITES]->(src:Source)
OPTIONAL MATCH (c)-[:HAS_COUNTER]->(cc)
RETURN e.edge_id, a.label_ko AS cause, b.label_ko AS effect, e.sign, e.empirical_sign, e.edge_type,
       c.mechanism, c.conditions, count(cc) AS counterconditions, src.title, src.source_class, c.review_status
ORDER BY e.edge_type, e.edge_id;


// ───────── 색 표현 화면: HTML 뷰어와 같은 색 규칙 ─────────
// 화면에 보여 줄 때만 가상 노드·가상 관계(APOC)를 만든다. DB에 저장된 데이터는 바뀌지 않는다.
//   노드 라벨: Value(가치 노드 10개, 금색) / LX(회녹색) / Variable(진녹색) - 노드마다 라벨 하나
//   관계 타입: POS(+ 초록) / NEG(- 빨강) / SIGNAL_POS, SIGNAL_NEG(signal, 연한 색 = HTML의 점선)
// 색은 Browser에서 한 번 지정한다 (neo4j/README.md 의 "색 지정" 참고).
// 아래 V1~V3는 첫 MATCH 부분만 다르고 뒷부분(WITH collect(e) 이후)은 같다.

// V1. 가치 중심 (색 표현)
MATCH (r:Variable) WHERE r.node_id IN ['firm_value', 'equity_value', 'expected_fcff']
MATCH (r)-[:CAUSES]-(n)
WITH collect(DISTINCT r) + collect(DISTINCT n) AS ns
MATCH (a)-[e:CAUSES]->(b) WHERE a IN ns AND b IN ns
WITH collect(e) AS rels
WITH rels, apoc.coll.toSet(apoc.coll.flatten([x IN rels | [startNode(x), endNode(x)]])) AS ns
WITH rels, apoc.map.fromLists([n IN ns | n.node_id],
       [n IN ns | apoc.create.vNode([CASE WHEN n:Value THEN 'Value' WHEN n:LX THEN 'LX' ELSE 'Variable' END], properties(n))]) AS vm
UNWIND rels AS e
WITH vm[startNode(e).node_id] AS a, vm[endNode(e).node_id] AS b, e
RETURN a, b, apoc.create.vRelationship(a,
       CASE WHEN e.edge_type = 'signal' THEN 'SIGNAL_' ELSE '' END + CASE e.sign WHEN '+' THEN 'POS' ELSE 'NEG' END,
       properties(e), b) AS r;

// V2. 재무 연결 (색 표현)
MATCH (a:Variable)-[e:CAUSES]->(b:Variable)
WHERE NOT a.node_id STARTS WITH 'n_' AND NOT b.node_id STARTS WITH 'n_'
WITH collect(e) AS rels
WITH rels, apoc.coll.toSet(apoc.coll.flatten([x IN rels | [startNode(x), endNode(x)]])) AS ns
WITH rels, apoc.map.fromLists([n IN ns | n.node_id],
       [n IN ns | apoc.create.vNode([CASE WHEN n:Value THEN 'Value' WHEN n:LX THEN 'LX' ELSE 'Variable' END], properties(n))]) AS vm
UNWIND rels AS e
WITH vm[startNode(e).node_id] AS a, vm[endNode(e).node_id] AS b, e
RETURN a, b, apoc.create.vRelationship(a,
       CASE WHEN e.edge_type = 'signal' THEN 'SIGNAL_' ELSE '' END + CASE e.sign WHEN '+' THEN 'POS' ELSE 'NEG' END,
       properties(e), b) AS r;

// V3. 전체 그래프 (색 표현, Initial Node Display 1000 필요)
MATCH (a:Variable)-[e:CAUSES]->(b:Variable)
WITH collect(e) AS rels
WITH rels, apoc.coll.toSet(apoc.coll.flatten([x IN rels | [startNode(x), endNode(x)]])) AS ns
WITH rels, apoc.map.fromLists([n IN ns | n.node_id],
       [n IN ns | apoc.create.vNode([CASE WHEN n:Value THEN 'Value' WHEN n:LX THEN 'LX' ELSE 'Variable' END], properties(n))]) AS vm
UNWIND rels AS e
WITH vm[startNode(e).node_id] AS a, vm[endNode(e).node_id] AS b, e
RETURN a, b, apoc.create.vRelationship(a,
       CASE WHEN e.edge_type = 'signal' THEN 'SIGNAL_' ELSE '' END + CASE e.sign WHEN '+' THEN 'POS' ELSE 'NEG' END,
       properties(e), b) AS r;


// ───────── 발표용: 카드를 그래프로 보기 ─────────
// HTML의 카드 패널(설명·성립 조건·깨지는 조건·출처)을 그래프로 그린다. 가상 노드라 DB는 바뀌지 않는다.
// 노드를 클릭하면 오른쪽에 전체 문장이 나온다.
// 카드 고르기: P0로 edge_id를 찾고 :param {eid: '...'} 로 바꾼 뒤 P1을 실행한다.

// P0. 관계 찾기: 원인·결과 이름에 들어간 글자로 검색 ($q 사용)
MATCH (a:Variable)-[e:CAUSES]->(b:Variable)
WHERE a.label_ko CONTAINS $q OR b.label_ko CONTAINS $q
RETURN e.edge_id, a.label_ko AS cause, b.label_ko AS effect, e.sign, e.edge_type ORDER BY e.edge_type, e.edge_id LIMIT 50;

// P1. 카드 보기 ($eid 사용)
MATCH (c:EdgeCard {edge_id: $eid})-[:CAUSE]->(a), (c)-[:EFFECT]->(b), (c)-[:CITES]->(s)
OPTIONAL MATCH (c)-[:HAS_COUNTER]->(cc)
OPTIONAL MATCH (cc)-[:CHECKS]->(v)
WITH c, a, b, s, collect(CASE WHEN cc IS NULL THEN null ELSE {cc: cc, v: v} END) AS ccs
WITH c, a, b, s, ccs,
     apoc.create.vNode(['카드'], {name: a.label_ko + ' → ' + b.label_ko, edge_id: c.edge_id,
       이론부호: c.sign, 실증부호: c.empirical_sign, 검토상태: c.review_status}) AS card,
     apoc.create.vNode([CASE WHEN a:Value THEN 'Value' WHEN a:LX THEN 'LX' ELSE 'Variable' END],
       {name: a.label_ko, label_ko: a.label_ko, node_id: a.node_id}) AS va,
     apoc.create.vNode([CASE WHEN b:Value THEN 'Value' WHEN b:LX THEN 'LX' ELSE 'Variable' END],
       {name: b.label_ko, label_ko: b.label_ko, node_id: b.node_id}) AS vb,
     apoc.create.vNode(['메커니즘'], {name: c.mechanism}) AS mech,
     apoc.create.vNode(['출처'], {name: s.title, 저자: s.author, 구분: s.source_class}) AS src
WITH c, card, va, vb, mech, src,
     [i IN range(0, size(c.conditions) - 1) |
        apoc.create.vNode(['성립조건'], {name: CASE WHEN i < 2 THEN c.conditions[i] ELSE '연구 맥락 ' + (i - 1) END,
                                         text: c.conditions[i]})] AS conds,
     [x IN ccs |
        [apoc.create.vNode(['깨지는조건'], {
            name: CASE WHEN x.v IS NULL THEN '맥락 조건'
                       ELSE x.v.label_ko + CASE x.cc.check_direction WHEN '+' THEN ' 상승 시' WHEN '-' THEN ' 하락 시' ELSE '' END END,
            text: x.cc.text, check_direction: x.cc.check_direction}),
         CASE WHEN x.v IS NULL THEN null
              ELSE apoc.create.vNode([CASE WHEN x.v:Value THEN 'Value' WHEN x.v:LX THEN 'LX' ELSE 'Variable' END],
                                     {name: x.v.label_ko, label_ko: x.v.label_ko, node_id: x.v.node_id}) END]] AS cps
RETURN
  [card, va, vb, mech, src] + conds + [p IN cps | p[0]] + [p IN cps WHERE p[1] IS NOT NULL | p[1]] AS nodes,
  [apoc.create.vRelationship(va, CASE c.sign WHEN '+' THEN 'POS' ELSE 'NEG' END, {}, vb),
   apoc.create.vRelationship(card, '원인', {}, va),
   apoc.create.vRelationship(card, '결과', {}, vb),
   apoc.create.vRelationship(card, '설명', {}, mech),
   apoc.create.vRelationship(card, '근거', {}, src)]
  + [n IN conds | apoc.create.vRelationship(card, '성립조건', {}, n)]
  + [p IN cps | apoc.create.vRelationship(card, '깨지는조건', {}, p[0])]
  + [p IN cps WHERE p[1] IS NOT NULL | apoc.create.vRelationship(p[0], '확인', {}, p[1])] AS rels;


// ───────── 발표 데모: 주장 구절 → 벡터 검색 → 그래프 경로 → 반박 확인 변수 ─────────
// 필요: embed_neo4j.py 실행(임베딩·벡터 인덱스), GenAI 플러그인, 발표 전에 Browser에서 한 번
//   :param {openai_key: 'sk-...'}
// 원인·결과는 "원자재 가격", "영업이익률"처럼 명사 구절로 넣는다. "상승/증가"를 붙이면 엉뚱한 연구 변수에 붙을 수 있다.
// 검증한 예: (매출 증가, 주주가치) / (원자재 가격, 영업이익률). 금리·부채 쪽은 연구 변수가 먼저 잡혀 결과가 약하다.

// D1. 주장 → 그래프 (발표 데모)
// 첫 줄의 원인·결과 구절만 바꿔서 실행한다. $openai_key 는 발표 전에 :param 으로 설정해 둔다.
WITH '매출 증가' AS cause_text, '주주가치' AS effect_text
WITH genai.vector.encode(cause_text, 'OpenAI', {token: $openai_key, model: 'text-embedding-3-small'}) AS vc,
     genai.vector.encode(effect_text, 'OpenAI', {token: $openai_key, model: 'text-embedding-3-small'}) AS ve
CALL db.index.vector.queryNodes('variable_embedding', 30, vc) YIELD node AS ca, score AS sa
WITH ve, collect({n: ca, s: sa}) AS cs
CALL db.index.vector.queryNodes('variable_embedding', 30, ve) YIELD node AS ef, score AS sb
WITH cs, collect({n: ef, s: sb}) AS es
UNWIND cs AS c
UNWIND es AS e
WITH c.n AS ca, c.s AS sa, e.n AS ef, e.s AS sb WHERE ca <> ef
MATCH p = (ca)-[:CAUSES*1..7]->(ef)
WITH p, ca, ef, sa + sb - 0.01 * length(p)
       + CASE WHEN ca:LX THEN 0 ELSE 0.03 END + CASE WHEN ef:LX THEN 0 ELSE 0.03 END AS score ORDER BY score DESC LIMIT 1
WITH nodes(p) AS ns, relationships(p) AS rs
CALL (ns, rs) {
  UNWIND rs AS r
  MATCH (:EdgeCard {edge_id: r.edge_id})-[:HAS_COUNTER]->(cc)-[:CHECKS]->(v)
  WHERE NOT v IN ns
  WITH v, cc.check_direction AS dir, collect(DISTINCT endNode(r)) AS targets
  ORDER BY CASE WHEN v:LX THEN 1 ELSE 0 END, size(targets) DESC, v.label_ko LIMIT 10
  RETURN collect({v: v, dir: dir, targets: targets}) AS checks
}
WITH rs, checks,
     apoc.map.fromLists([n IN ns | n.node_id],
       [n IN ns | apoc.create.vNode([CASE WHEN n:Value THEN 'Value' WHEN n:LX THEN 'LX' ELSE 'Variable' END],
                                    {name: n.label_ko, label_ko: n.label_ko, node_id: n.node_id})]) AS vm
WITH rs, checks, vm,
     [x IN checks | apoc.create.vNode(['확인변수'], {
        name: x.v.label_ko + CASE x.dir WHEN '+' THEN ' 상승' WHEN '-' THEN ' 하락' ELSE '' END,
        label_ko: x.v.label_ko, 방향: x.dir, 걸리는_관계_수: size(x.targets)})] AS cvs
RETURN [k IN keys(vm) | vm[k]] + cvs AS nodes,
       [r IN rs | apoc.create.vRelationship(vm[startNode(r).node_id], CASE r.sign WHEN '+' THEN 'POS' ELSE 'NEG' END,
                                            {edge_id: r.edge_id}, vm[endNode(r).node_id])]
       + apoc.coll.flatten([i IN range(0, size(checks) - 1) |
           [t IN checks[i].targets | apoc.create.vRelationship(cvs[i], '상쇄', {}, vm[t.node_id])]]) AS rels;

// D2. 주장 → 표: 찾은 경로, 누적 부호, 반박할 때 확인할 변수 (D1과 같은 구절을 넣는다)
WITH '매출 증가' AS cause_text, '주주가치' AS effect_text
WITH genai.vector.encode(cause_text, 'OpenAI', {token: $openai_key, model: 'text-embedding-3-small'}) AS vc,
     genai.vector.encode(effect_text, 'OpenAI', {token: $openai_key, model: 'text-embedding-3-small'}) AS ve
CALL db.index.vector.queryNodes('variable_embedding', 30, vc) YIELD node AS ca, score AS sa
WITH ve, collect({n: ca, s: sa}) AS cs
CALL db.index.vector.queryNodes('variable_embedding', 30, ve) YIELD node AS ef, score AS sb
WITH cs, collect({n: ef, s: sb}) AS es
UNWIND cs AS c
UNWIND es AS e
WITH c.n AS ca, c.s AS sa, e.n AS ef, e.s AS sb WHERE ca <> ef
MATCH p = (ca)-[:CAUSES*1..7]->(ef)
WITH p, ca, ef, sa + sb - 0.01 * length(p)
       + CASE WHEN ca:LX THEN 0 ELSE 0.03 END + CASE WHEN ef:LX THEN 0 ELSE 0.03 END AS score ORDER BY score DESC LIMIT 1
WITH p, nodes(p) AS ns, relationships(p) AS rs,
     reduce(x = 1, r IN relationships(p) | x * CASE r.sign WHEN '+' THEN 1 ELSE -1 END) AS net
UNWIND rs AS r
OPTIONAL MATCH (:EdgeCard {edge_id: r.edge_id})-[:HAS_COUNTER]->(cc)-[:CHECKS]->(v)
WHERE NOT v IN ns
WITH ns, net, v, cc.check_direction AS dir,
     CASE WHEN v IS NULL THEN 2 WHEN v:LX THEN 1 ELSE 0 END AS pri,
     count(DISTINCT r) AS k, collect(DISTINCT cc.text)[0] AS ex
ORDER BY pri, k DESC, v.label_ko LIMIT 15
RETURN reduce(t = '', n IN ns | t + CASE WHEN t = '' THEN '' ELSE ' → ' END + n.label_ko) AS 찾은_경로,
       CASE net WHEN 1 THEN '+' ELSE '-' END AS 누적_부호,
       v.label_ko AS 확인할_변수,
       CASE dir WHEN '+' THEN '상승 시 상쇄' WHEN '-' THEN '하락 시 상쇄' ELSE '' END AS 방향,
       CASE WHEN v IS NULL THEN 0 ELSE k END AS 걸리는_관계_수,
       ex AS 예시;
