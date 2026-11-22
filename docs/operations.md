# 서서히 backend handoff

## Current setup (2026-11)

- **Scheduler:** the worker runs on the owner's Mac through launchd (`ops/live/com.jihaein.ies-refresh.plist`),
  weekdays 08:30, 19:30 and 22:30 KST. Each run is `ops/live/run_daily.sh`: restore missing files from cloud
  storage, run one `scheduled_refresh --once` tick, upload results, and raise a macOS notification on failure
  or when the latest session is not published.
- **Serving database:** Supabase project `investment-evidence-project`, migrated with `001_serving.sql`
  (`ops/live/migrate.py` checks keys and RLS and backs up before applying).
- **Files:** every data folder is mirrored to the private Supabase Storage bucket `ies-data` by
  `ops/cloud/sync.py` (folders listed in `ops/cloud/datasets.json`). `ops/cloud/restore_test.py` restores
  into an empty folder and re-computes the published signals to prove the copy is complete.
- **Inputs to maintain:** `ops/live/krx_sessions.csv` (extend before 2026-12-30) and
  `ops/live/stock_coverage.csv` (end a stock's interval when it is delisted or suspended).
- **Held filings:** `pending_triage.json` in `DART_SNAPSHOT_DIR`. Only `needs_review` items alert.

The sections below describe the general (hosted) deployment.

## Stage the service

- Use Python 3.12 and install `requirements.txt` for the API and worker. Install `requirements-dev.txt` for tests.
- Create an empty staging PostgreSQL database and apply `data_pipeline/migrations/001_serving.sql` as the database owner. For an existing database, verify the column types and unique keys and take a backup before applying the same migration.
- Use `.env.example` to set the worker, API and input configuration. Give the API role `SELECT` on `stocks`, `prices` and `signals`, and column-level `SELECT` on `refresh_runs(price_session, financial_as_of, reverted_at)`. Create `FOR SELECT TO <reader> USING (true)` RLS policies for this role on the four tables. Give the worker write access to the serving tables and its release log. Use `API_READ_TOKEN` for private access and set request limits at the ingress. Confirm direct Supabase access matches the intended audience.
- Stage the historical handoff in `RAWDATA_DIR`. It needs the two-header security master, market CSVs and the point-in-time snapshot tree used by the existing models. Run `python -m data_pipeline.seed.run_pipeline` to seed the historical data. `--prices` imports files already on disk.
- Provide an exchange calendar CSV with a `session` column containing actual KRX session dates, including upcoming sessions. Provide a coverage CSV with `stock_code,active_from,active_until` columns. Active intervals include the start and exclude the end. Use approved listing and suspension data to maintain expected coverage. The optional delisting file has `stock_code,delisted_on` columns and is loaded by the stock-master import.
- Run `python -m uvicorn backend.api:app --host 0.0.0.0` for staging. `GET /` indicates liveness, `GET /ready` reports serving readiness and missed market sessions, and `POST /portfolio` returns a result in input order with evaluation dates, versions, hold reasons and price and filing freshness.

## Run the automatic refreshes

Start one worker under your service manager using `python -m data_pipeline.scheduled_refresh`.
The worker checks KRX every 15 minutes and polls DART hourly in Asia/Seoul. KRX publication
starts at the configured close-data cutoff and works through missed exchange sessions in order.
DART resumes from a durable cursor and rereads the most recent seven days for reconciliation.
Set `MARKET_START_SESSION` to the first close to refresh after seeding. Set `DART_START_DATE`
after the first seed snapshot, and set `DART_SNAPSHOT_DIR` to a durable worker-owned directory
outside the historical tree. Confirm the live provider's release time and the coverage file before enabling jobs.

### DART conversion and exceptions

The worker collects annual, half-year and quarterly filings, including amendments. It uses
the filing title and the company's fiscal calendar to identify the period. Financial statements
must match the exact receipt ID. The worker prefers consolidated figures and uses standalone
figures when DART confirms the consolidated response is absent. It archives each verifiable
version before using the existing formulas to build Ohlson, DuPont and annual F-Score inputs.

A new snapshot becomes eligible on the next exchange session. Late arrivals use the latest
published financial cutoff when necessary. The worker replaces the applicable period, refreshes
its affected dependencies and reevaluates all covered stocks so DuPont peer comparisons can change.
Earlier snapshots remain queryable. Same-session files can receive a new release ID as further
filings arrive. The previous release remains available for rollback.

Check `pending.json` for unavailable statement versions, unsupported filing metadata and withdrawals.
OpenDART's financial endpoint returns a period-level result. If it returns the wrong receipt,
preserve the pending item and obtain a verified version for that exact receipt. Review
`review.json` and `lineage.json` in generated snapshots for missing or held fields and their
inputs. Annual F-Score uses verified comparatives, reconciled long-term debt and an
exact-receipt common-share issuance table. Missing evidence holds the applicable score.
A delayed or ambiguous annual supplement can be resolved through a reviewed PIT release.

For exceptional corrections, publish a reviewed full PIT snapshot with
`python -m data_pipeline.live_refresh financial <bundle>`. The bundle uses the same model
directories as the historical tree and an `approved.json` containing `effective_session`,
`approved_by`, `receipts` and `sha256` for each model file. The older six-CSV preprocessor
remains available for review exports.

## Isolated staging

Use [the staging Compose file](../ops/staging/compose.yaml) to provision a localhost-only
PostgreSQL instance. Set a staging-only password and start the database with
`docker compose -f ops/staging/compose.yaml up -d --wait`. The initial migration runs on a
new volume. Configure `STAGING_DB_URL` explicitly, seed the staging tables using that same
connection, and point `RAWDATA_DIR`, the calendar, the coverage file and `DART_SNAPSHOT_DIR`
at staging inputs.

- Start both timers with `python -m data_pipeline.scheduled_refresh --staging`.
- For a replay, use staged `krx_session_YYYY-MM-DD.csv` files and archived DART payloads. Run
  `python -m data_pipeline.scheduled_refresh --staging --offline --once --as-of YYYY-MM-DD`
  for one scheduled tick, or use `--offline` without `--once` for recurring ticks.
- Set `API_DB_URL` to the staging URL and bind the local API to loopback. If the staging database
  is disposable, its owner may seed and replay the data. Use separate database roles in a shared environment.
- Check `/signal` and `/portfolio` before and after a filing amendment and repeat the tick.
  Compare filing receipt IDs, evaluation dates, price dates and release IDs. Alert if either
  job fails, pending receipts age or the latest KRX session falls behind.

## Recover and verify

- Rerun a failed job after correcting its input. Market jobs advance session by session and fail on invalid or incomplete batches. For a staged CSV replay use `python -m data_pipeline.live_refresh market --session YYYY-MM-DD --price-csv file.csv`. Use this on an unpublished session.
- Use `python -m data_pipeline.live_refresh rollback RELEASE_ID` to undo the latest publication and restore its prior serving rows. Pause the schedule during rollback and move a rejected approved bundle out of the inbox so it is not republished. Keep independent PostgreSQL backups and test restoring one.
- Run `python -m pytest`. Set `IES_TEST_DB_URL` to a disposable PostgreSQL database to run the integration test, which creates and removes an isolated schema. Replay a genuine KRX close and DART amendment against staging before launch. Verify the served score, source dates and release ID, then repeat the update and exercise rollback.
- The current default user-facing verdict is the existing sum-based rule, versioned as `seoseohi-sum-v1`. Get model-owner sign-off for the rule and DuPont reference parity. Use the saved `/signal` response for explanations. The price gate, 급락 verdicts and hosted LLM rebuttal still require their own release checks.

Assign named owners for market coverage, filing review, model rules, database recovery and on-call alerts before handoff.
