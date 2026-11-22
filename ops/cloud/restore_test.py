"""Prove the data lives in the cloud: restore into an empty folder, compare, and re-run the model.

    python ops/cloud/restore_test.py                     # live datasets (rawdata, dart_snapshots)
    python ops/cloud/restore_test.py --all               # every dataset (downloads ~0.8 GB)

1. Downloads the datasets from Supabase Storage into a NEW empty folder (nothing local is touched).
2. Checks every restored file against this computer's copy (same file list, same sha256).
3. Re-computes the published 서서히 signals for the latest evaluation date using ONLY the restored
   files, and compares scores and verdicts with what is in the database.
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops" / "cloud"))

from dotenv import load_dotenv  # noqa: E402

import sync  # noqa: E402

LIVE = ["rawdata", "dart_snapshots"]


def main():
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dest", help="empty folder to restore into (default: a new temp folder)")
    args = parser.parse_args()
    dest = Path(args.dest or tempfile.mkdtemp(prefix="ies-restore-test-"))
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"{dest} is not empty")
    store = sync.Store(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                       os.getenv("CLOUD_BUCKET", "ies-data"))
    datasets = sync.load_datasets(None if args.all else LIVE)
    cache = sync.HashCache()
    print(f"Restoring {[d['name'] for d in datasets]} into {dest}")
    sync.pull(store, datasets, cache, dest=str(dest))

    # 2) file-by-file comparison with this computer
    problems = 0
    for dataset in datasets:
        _, local = sync.local_manifest(dataset, cache)
        restored = {**dataset, "root": dest / dataset["name"]}
        _, copy = sync.local_manifest(restored, cache)
        missing = sorted(set(local) - set(copy))
        differ = sorted(r for r in set(local) & set(copy) if local[r]["sha256"] != copy[r]["sha256"])
        state = "IDENTICAL" if not (missing or differ) else "MISMATCH"
        problems += bool(missing or differ)
        print(f"{dataset['name']:<16} {state}: {len(copy):,} restored / {len(local):,} local"
              + (f", missing {len(missing)} e.g. {missing[:2]}" if missing else "")
              + (f", different {len(differ)} e.g. {differ[:2]}" if differ else ""))
    cache.save()

    # 3) re-run the model from the restored copy only
    if {"rawdata", "dart_snapshots"} <= {d["name"] for d in datasets}:
        problems += model_check(dest)
    print("\nRESTORE TEST PASSED" if not problems else f"\nRESTORE TEST FAILED ({problems} problem(s))")
    print(f"(restored copy left at {dest}; delete it when done)")
    sys.exit(1 if problems else 0)


def model_check(dest):
    from sqlalchemy import create_engine, text

    os.environ["DART_SNAPSHOT_DIR"] = str(dest / "dart_snapshots")      # resolve snapshots from the copy
    os.environ["RAWDATA_DIR"] = str(dest / "rawdata")
    from data_pipeline.live_refresh import financial_catalog, scoring_inputs
    from models.seoseohi.ohlson.ohlson import load_mktcap
    from models.seoseohi.pipeline import integrate_sum, load_data_pit, run_seoseohi_pit

    base = dest / "rawdata"
    engine = create_engine(os.environ["SUPABASE_DB_URL"], hide_parameters=True)
    with engine.connect() as connection:
        raw = connection.execute(text("""SELECT financial_as_of, snapshot_path FROM refresh_runs
                                         WHERE reverted_at IS NULL AND snapshot_path IS NOT NULL""")).all()
        catalog = {}
        for day, path in raw:                       # force every snapshot to come from the restored copy
            restored = dest / "dart_snapshots" / Path(path).name
            if not restored.is_dir():
                print(f"model check: snapshot {Path(path).name} missing from the restored copy")
                return 1
            catalog[day] = str(restored)
        assert all(str(dest) in p for p in catalog.values())
        latest = connection.scalar(text("SELECT max(eval_date) FROM signals"))
        baseline = connection.scalar(text("SELECT max(baseline_date) FROM signals WHERE eval_date = :d "
                                          "AND baseline_date < :d"), {"d": latest})
        published = {r.stock_code: r for r in connection.execute(text(
            "SELECT stock_code, dupont_score, ohlson_score, fscore, verdict_sum FROM signals "
            "WHERE eval_date = :d AND baseline_date = :b"), {"d": latest, "b": baseline})}
    print(f"\nModel check: recomputing {len(published)} signals for eval {latest}, baseline {baseline} "
          "from the restored files only")
    mismatches = []
    with scoring_inputs(base, catalog) as view:
        data = load_data_pit(str(view))
        data.update(market=load_mktcap(view), median_cache={})
        for code, row in published.items():
            result = run_seoseohi_pit(data, {"stock_code": code, "buy_date": baseline}, latest)
            got = (result["scores"]["dupont"], result["scores"]["ohlson"], result["scores"]["fscore"],
                   integrate_sum(result["scores"]))
            got = tuple(None if v is None or v != v else (int(v) if not isinstance(v, str) else v) for v in got)
            want = (row.dupont_score, row.ohlson_score, row.fscore, row.verdict_sum)
            if got != want:
                mismatches.append((code, want, got))
    print(f"model check: {len(published) - len(mismatches)}/{len(published)} identical to the database"
          + (f"; first mismatches {mismatches[:3]}" if mismatches else ""))
    return 1 if mismatches else 0


if __name__ == "__main__":
    main()
