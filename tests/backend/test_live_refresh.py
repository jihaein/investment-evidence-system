import csv
import hashlib
import json
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from backend import api
from data_pipeline import live_refresh
from data_pipeline.publication import publish, rollback_latest


@pytest.fixture
def database():
    url = os.getenv("IES_TEST_DB_URL")
    if not url:
        pytest.skip("Set IES_TEST_DB_URL to a disposable PostgreSQL database")
    engine = create_engine(url)
    namespace = "ies_test_" + uuid4().hex
    with engine.begin() as con:
        con.execute(text(f"CREATE SCHEMA {namespace}"))
    isolated = create_engine(url, connect_args={"options": f"-c search_path={namespace}"})
    try:
        migration = Path(__file__).resolve().parents[2] / "data_pipeline/migrations/001_serving.sql"
        with isolated.connect() as con:
            con.exec_driver_sql(migration.read_text())
            con.commit()
        yield isolated
    finally:
        isolated.dispose()
        with engine.begin() as con:
            con.execute(text(f"DROP SCHEMA {namespace} CASCADE"))
        engine.dispose()


def write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)


def test_publish_portfolio_amendment_and_rollback(database, tmp_path, monkeypatch):
    day1, day2, day3 = (date.fromisoformat(day) for day in ("2025-01-06", "2025-01-07", "2025-01-08"))
    base = tmp_path / "inputs"
    stock = "010170"
    receipt = "20250107000001"
    old = base / live_refresh.SNAPSHOTS / "2025-01-02"
    (old / "FScore").mkdir(parents=True)
    coverage = tmp_path / "coverage.csv"
    write_csv(coverage, ("stock_code", "active_from", "active_until"), [(stock, "2024-01-01", "")])
    monkeypatch.setenv("MARKET_COVERAGE_CSV", str(coverage))
    monkeypatch.delenv("MARKET_CALENDAR_CSV", raising=False)
    monkeypatch.delenv("API_READ_TOKEN", raising=False)
    monkeypatch.setattr(api, "get_engine", lambda: database)
    monkeypatch.setattr(live_refresh, "load_master", lambda _: {stock: ("Test Company", "KOSDAQ")})
    monkeypatch.setattr(live_refresh, "pull_day", lambda day, _: [{
        "stock_code": stock, "stock_name_2014": "Test Company", "MKT_NM": "KOSDAQ",
        "BAS_DD": day.strftime("%Y%m%d"), "MKTCAP": 10000, "LIST_SHRS": 100,
        "TDD_CLSPRC": 100, "quality_status": "OK",
    }])

    def score(view, codes, days):
        amendment = (view / live_refresh.SNAPSHOTS / day3.isoformat()).is_dir()
        return [{
            "stock_code": code, "baseline_date": date(2025, 1, 2), "eval_date": day,
            "dupont_score": 3 if amendment else 1, "ohlson_score": 1, "fscore": 0,
            "sum_total": 4 if amendment else 2, "verdict_count": "주의" if amendment else "No",
            "verdict_sum": "Yes(훼손 우려)" if amendment else "No",
            "rule_version": "seoseohi-sum-v1",
            "evidence": {"financial_as_of": "2025-01-08" if amendment else "2025-01-02"},
        } for code in codes for day in days]

    monkeypatch.setattr(live_refresh, "compute", score)
    dates = [day1, day2, day3]
    live_refresh.refresh_prices(database, base, dates, exact=day1)
    live_refresh.refresh_prices(database, base, dates, exact=day1)
    live_refresh.refresh_prices(database, base, dates, exact=day2)

    client = TestClient(api.app)
    holding = {"stock_code": stock, "buy_date": "2025-01-02"}
    published = client.post("/portfolio", json=[holding, holding]).json()
    assert len(published) == 2
    assert published[0]["price_as_of"] == day2.isoformat()
    assert published[0]["eval_date"] == day2.isoformat()
    assert published[0]["verdict"] == "No"
    assert client.post("/portfolio?as_of=2025-01-01", json=[holding]).status_code == 422
    assert client.get("/signal", params={"stock_code": "bad", "buy_date": "2025-01-02"}).status_code == 422

    archived = base / "_raw_pull/dart/receipts" / f"{receipt}.json"
    archived.parent.mkdir(parents=True)
    archived.write_text(json.dumps({"rcept_no": receipt, "rcept_dt": "20250107"}))
    snapshot = tmp_path / "approved"
    hashes = {}
    for name in live_refresh.SNAPSHOT_FILES:
        file = snapshot / name
        write_csv(file, ("rcept_no", "disclosed_date"), [("receipt", "date"), (receipt, "2025-01-07")])
        hashes[name] = hashlib.sha256(file.read_bytes()).hexdigest()
    (snapshot / "approved.json").write_text(json.dumps({
        "effective_session": day3.isoformat(), "receipts": [receipt],
        "approved_by": "test reviewer", "sha256": hashes,
    }))

    # The close was already served before the approved amendment arrived.
    live_refresh.refresh_prices(database, base, dates, exact=day3)
    live_refresh.refresh_financials(database, base, snapshot, dates)
    current = client.post("/portfolio", json=[holding]).json()[0]
    assert current["verdict"] == "Yes(훼손 우려)"
    assert current["financial_as_of"] == day3.isoformat()
    before_amendment = client.post(f"/portfolio?as_of={day2}", json=[holding]).json()[0]
    assert before_amendment["verdict"] == "No"

    with pytest.raises(RuntimeError, match="abort"), database.begin() as con:
        publish(con, "doomed-release", [], score(base, [stock], [day3]))
        raise RuntimeError("abort")
    assert client.post("/portfolio", json=[holding]).json()[0]["release_id"] == current["release_id"]

    with database.begin() as con:
        rollback_latest(con, current["release_id"])
    restored = client.post("/portfolio", json=[holding]).json()[0]
    assert restored["verdict"] == "No"
    assert restored["price_as_of"] == day3.isoformat()


def test_incomplete_market_batch_keeps_original_archive(tmp_path):
    day = date(2025, 1, 6)
    path = tmp_path / "input.csv"
    write_csv(path, ("stock_code", "stock_name_2014", "MKT_NM", "BAS_DD", "MKTCAP",
                     "LIST_SHRS", "TDD_CLSPRC", "quality_status"),
              [("010170", "Test Company", "KOSDAQ", "20250106", 10000, 100, 100, "OK")])
    with pytest.raises(ValueError, match="incomplete coverage"):
        live_refresh.price_batch(tmp_path, day, {"010170", "010171"},
                                 {"010170": None, "010171": None}, path)
    assert not (tmp_path / live_refresh.MARKET_DIR).exists()
