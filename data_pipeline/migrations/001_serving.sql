BEGIN;

CREATE TABLE IF NOT EXISTS stocks (
    stock_code text PRIMARY KEY,
    stock_name text,
    market text,
    ksic_code text,
    corp_code text,
    delisted_on date
);
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS corp_code text;
ALTER TABLE stocks ADD COLUMN IF NOT EXISTS delisted_on date;

CREATE TABLE IF NOT EXISTS prices (
    stock_code text NOT NULL REFERENCES stocks(stock_code),
    bas_dd date NOT NULL,
    market text,
    mktcap bigint,
    list_shrs bigint,
    close_prc numeric,
    quality_status text,
    PRIMARY KEY (stock_code, bas_dd)
);

CREATE TABLE IF NOT EXISTS signals (
    stock_code text NOT NULL REFERENCES stocks(stock_code),
    baseline_date date NOT NULL,
    eval_date date NOT NULL,
    dupont_score integer,
    ohlson_score integer,
    fscore integer,
    sum_total integer,
    verdict_count text,
    verdict_sum text,
    PRIMARY KEY (stock_code, baseline_date, eval_date)
);

CREATE TABLE IF NOT EXISTS refresh_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_key text NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now(),
    reverted_at timestamptz,
    price_session date,
    snapshot_path text,
    financial_as_of date,
    before_prices jsonb NOT NULL,
    before_signals jsonb NOT NULL,
    price_keys jsonb NOT NULL,
    signal_keys jsonb NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_refresh_runs_job ON refresh_runs(job_key)
    WHERE reverted_at IS NULL;

ALTER TABLE signals ADD COLUMN IF NOT EXISTS rule_version text;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS evidence jsonb;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS release_id bigint REFERENCES refresh_runs(id);

ALTER TABLE stocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE prices ENABLE ROW LEVEL SECURITY;
ALTER TABLE signals ENABLE ROW LEVEL SECURITY;
ALTER TABLE refresh_runs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON refresh_runs FROM PUBLIC;

COMMIT;
