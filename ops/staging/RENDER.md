# Hosted staging on Render and Supabase

Use the separate Render Staging environment and a dedicated Supabase staging project. Use
a deployment branch containing this checkout. In Render, choose `ops/staging/render.yaml`
as the Blueprint path. It defines a free web service and a 2 GB background worker with 5 GB
of persistent storage. The services use the Singapore region. Verify provider availability and
the database region before selecting a location in the dashboard.

## Seed and start the jobs

1. Apply `data_pipeline/migrations/001_serving.sql` to the staging database. Set the web service
   `API_DB_URL` to its read-only staging role and the worker `STAGING_DB_URL` to its staging
   write role. The staging API uses a generated bearer token. Role grants are described in
   [the operations runbook](../../docs/operations.md).
2. Set the worker's KRX and OpenDART credentials in Render secrets. On the worker disk, seed
   `/var/data/rawdata` with the preserved historical model inputs. Put the verified KRX calendar at
   `/var/data/krx_sessions.csv` and expected universe at `/var/data/stock_coverage.csv`. Add that
   same calendar as `krx_sessions.csv` in the API's Render Secret Files.
3. In the worker shell, use `SUPABASE_DB_URL="$STAGING_DB_URL" python -m data_pipeline.load_stocks`
   to seed the master and `SUPABASE_DB_URL="$STAGING_DB_URL" python -m data_pipeline.load_prices`
   to seed historical prices. Set the start dates to the selected staging window. Exclude replay
   price batches and receipt files for the sessions chosen as live acceptance cases.
4. Create `/var/data/.staging-seed-ready`. The worker starts the existing scheduler after that
   marker appears. DART polls hourly and KRX checks every 15 minutes after the close-data cutoff.
   Check the worker log for a live fetch and the resulting release IDs.

## Verify live serving

Use one covered stock whose Ohlson model uses the KRX close and a stock whose current evidence
should use a new DART receipt. Export the staging API token locally and run the verifier.

```bash
python ops/staging/verify_live.py "$API_URL" \
  --market-stock "$MARKET_STOCK" --market-session "$MARKET_SESSION" \
  --filing-stock "$FILING_STOCK" --receipt "$DART_RECEIPT" --buy-date "$BUY_DATE"
```

Record the two release IDs alongside the worker's provider logs. Compare the filing stock's
earlier `/signal` response if verifying an amendment. A source that has published no eligible
filing in the staging window leaves the corresponding live check pending.
