-- 002: FDS 공통 인과그래프 (LLM 분석의 경제명제). backend/graph/load_graph.py가 채운다.
-- 원본은 저장소의 data/fds_graph/ (graph.json + edge_cards/). 표는 조회용 사본이라 적재할 때마다 통째로 바꾼다.
BEGIN;

CREATE TABLE IF NOT EXISTS fds_graph_meta (
    graph_version text NOT NULL,
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    variables     integer NOT NULL,
    edges         integer NOT NULL
);

CREATE TABLE IF NOT EXISTS fds_variables (
    node_id         text PRIMARY KEY,
    layer           text NOT NULL,
    label_ko        text NOT NULL,
    synonyms_ko     text[] NOT NULL DEFAULT '{}',
    observable      boolean,
    source_variable text,
    unit            text,
    can_be_claim    boolean,
    is_value        boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS fds_edges (
    edge_id        text PRIMARY KEY,
    cause          text NOT NULL REFERENCES fds_variables(node_id),
    effect         text NOT NULL REFERENCES fds_variables(node_id),
    sign           text NOT NULL CHECK (sign IN ('+', '-')),
    edge_type      text,
    empirical_sign text,
    review_status  text,
    mechanism      text,
    conditions     text[] NOT NULL DEFAULT '{}',
    source_title   text,
    source_author  text,
    source_class   text
);

CREATE TABLE IF NOT EXISTS fds_counterconditions (
    cc_id           text PRIMARY KEY,
    edge_id         text NOT NULL REFERENCES fds_edges(edge_id) ON DELETE CASCADE,
    text            text NOT NULL,
    check_node      text REFERENCES fds_variables(node_id),
    check_direction text
);

-- 임베딩: kind = 'variable' | 'card', model = OpenAI 임베딩 모델 이름
CREATE TABLE IF NOT EXISTS fds_embeddings (
    kind   text NOT NULL,
    id     text NOT NULL,
    model  text NOT NULL,
    text   text NOT NULL,
    vector real[] NOT NULL,
    PRIMARY KEY (kind, id, model)
);

ALTER TABLE fds_graph_meta ENABLE ROW LEVEL SECURITY;
ALTER TABLE fds_variables ENABLE ROW LEVEL SECURITY;
ALTER TABLE fds_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE fds_counterconditions ENABLE ROW LEVEL SECURITY;
ALTER TABLE fds_embeddings ENABLE ROW LEVEL SECURITY;

COMMIT;
