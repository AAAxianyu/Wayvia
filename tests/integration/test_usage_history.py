"""Synthetic counters only: no native usage collection or real subscription publication."""

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from wayvia.api import create_app
from wayvia.config import WayviaConfig, example_config
from wayvia.services.control import ControlService, SimulatedDataplane
from wayvia.services.usage import UsageStore
from wayvia.services.usage_history import history, prune
from wayvia.storage import MIGRATIONS, Database


@pytest.fixture
def control(tmp_path):
    return ControlService(
        tmp_path,
        WayviaConfig.model_validate(example_config()),
        mode="test",
        dataplane=SimulatedDataplane(),
    )


def sample(device, at, up, *, generation="one", ident="c1", created=5900):
    return {
        "observed_at": at,
        "generation": generation,
        "closed_buffer_full": False,
        "oldest_closed_at": None,
        "connections": [
            {
                "id": ident,
                "device_id": device,
                "created_at": created,
                "closed_at": None,
                "upload": up,
                "download": 2 * up,
            }
        ],
    }


def query(control, device, start=6000, end=6060, grain="minute", now=6120):
    return history(control.db, device, start=start, end=end, grain=grain, now=now)


def test_baseline_delta_restart_and_missing_minutes(control):
    device = control.add("history", "desktop")["id"]
    store = UsageStore(control.db)
    store.record(sample(device, 6000, 100))
    store.record(sample(device, 6005, 130))
    store.record(sample(device, 6005, 999))  # duplicate and late records do not count twice
    store.record(sample(device, 6004, 999))
    bucket = query(control, device)["buckets"][0]
    assert bucket["upload_bytes"] == 30 and bucket["unallocated_upload_bytes"] == 100
    assert bucket["covered_seconds"] == 5 and bucket["connections_last"] == 1
    assert "baseline" in bucket["uncertainty"]
    # Reopen durable store, drop same-generation counters, then continue from the new watermark.
    store = UsageStore(control.db)
    store.record(sample(device, 6010, 4))
    store.record(sample(device, 6015, 7))
    bucket = query(control, device)["buckets"][0]
    assert bucket["upload_bytes"] == 33 and bucket["unallocated_upload_bytes"] == 104
    assert "counter_reset" in bucket["uncertainty"]
    assert store.device(device, now=6016)["upload_bytes"] == 137
    store.record(sample(device, 6120, 50, generation="two"))
    buckets = query(control, device, end=6180, now=6180)["buckets"]
    assert buckets[1]["status"] == "unknown" and buckets[1]["upload_bytes"] is None
    assert buckets[2]["unallocated_upload_bytes"] == 50
    assert "core_generation_changed" in buckets[2]["uncertainty"]
    assert store.device(device, now=6180)["online"] is None


def test_outage_and_cross_minute_attribution_are_explicit(control):
    device = control.add("minute", "desktop")["id"]
    store = UsageStore(control.db)
    store.record(sample(device, 6058, 100))
    store.record(sample(device, 6063, 120))
    rows = query(control, device, end=6120)["buckets"]
    assert rows[0]["covered_seconds"] == 2 and rows[1]["covered_seconds"] == 3
    assert rows[1]["upload_bytes"] == 20
    assert "cross_bucket_interval" in rows[1]["uncertainty"]
    store.failure("USAGE_UNAVAILABLE")
    store.record(sample(device, 6068, 160))
    row = query(control, device, start=6060, end=6120)["buckets"][0]
    assert row["unallocated_upload_bytes"] == 40 and row["covered_seconds"] == 3
    assert "collection_gap" in row["uncertainty"]


def test_new_short_connections_rotate_revoke_and_no_target_metadata(control):
    device = control.add("identity", "desktop")["id"]
    store = UsageStore(control.db)
    store.record(sample(device, 6000, 10))
    control.rotate(device)
    new = sample(device, 6005, 15, ident="new", created=6001)
    new["connections"][0].update(target="secret.example", body="PRIVATE")
    store.record(new)
    control.revoke_device(device)
    store.record(sample(device, 6010, 20, ident="new", created=6001))
    row = query(control, device)["buckets"][0]
    assert row["upload_bytes"] == 20 and row["connections_peak"] == 1
    assert row["connections_last"] == 0
    assert "secret.example" not in json.dumps(query(control, device))
    assert "connection_final_counter_missing" in row["uncertainty"]


def test_subscription_claims_separate_authorized_query_export(control):
    device = control.add("fetch", "desktop")["id"]
    issued = control.issue(device, "mihomo")
    public = TestClient(create_app(control, "a" * 48, subscriptions_only=True))
    assert public.get(issued["path"], headers={"User-Agent": "mihomo/1.19.32"}).status_code == 200
    assert (
        public.get(
            issued["path"], headers={"User-Agent": "secret-user https://private/?token=BAD"}
        ).status_code
        == 200
    )
    now = time.time()
    result = history(control.db, device, start=now - 60, end=now + 1)
    assert sum(r.get("subscription_fetches", 0) for r in result["buckets"]) == 2
    assert {c["client"] for c in result["subscription_client_claims"]} == {"mihomo", "unknown"}
    assert all(r["upload_bytes"] is None for r in result["buckets"])
    assert control.device(device)["usage"]["online"] is None
    encoded = json.dumps(result)
    for value in ("secret-user", "private", "BAD", issued["token"], "token_hash"):
        assert value not in encoded
    path = f"/api/v1/devices/{device}/history"
    params = {"since": now - 60, "until": now + 1}
    admin = TestClient(create_app(control, "a" * 48))
    assert admin.get(path, params=params).status_code == 401
    assert public.get(path, params=params).status_code == 404
    assert public.get(path + "/export", params=params).status_code == 404
    response = admin.get(
        path + "/export", params=params, headers={"Authorization": "Bearer " + "a" * 48}
    )
    assert response.status_code == 200 and "no-store" in response.headers["cache-control"]
    assert response.json()["device_id"] == device


def test_retention_hour_rollup_is_atomic_and_queries_bounded(control):
    device = control.add("retention", "desktop")["id"]
    store = UsageStore(control.db)
    store.record(sample(device, 6000, 100))
    store.record(sample(device, 6005, 125))
    with control.db.connect() as conn:
        prune(conn, 6005 + 31 * 86400)
        prune(conn, 6005 + 31 * 86400)
    assert query(control, device, now=6005 + 31 * 86400)["buckets"][0]["status"] == "expired"
    hourly = query(control, device, grain="hour", now=6005 + 31 * 86400)["buckets"][0]
    assert hourly["upload_bytes"] == 25 and hourly["unallocated_upload_bytes"] == 100
    with pytest.raises(Exception, match="shorter window"):
        history(control.db, device, start=0, end=10001 * 60, now=10001 * 60)
    for a, b in ((float("nan"), 1), (0, float("inf")), (10, 1)):
        with pytest.raises(Exception, match="Invalid usage"):
            history(control.db, device, start=a, end=b)


def test_migration_keeps_legacy_totals_without_fabricating_history(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
        for version in (1, 2, 3):
            conn.executescript(MIGRATIONS[version])
            conn.execute("INSERT INTO schema_migrations VALUES(?)", (version,))
        conn.execute("INSERT INTO settings VALUES('usage_state','{}')")
    db = Database(path)
    with db.connect() as conn:
        assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations")] == [1, 2, 3, 4]
        assert conn.execute("SELECT count(*) FROM usage_history").fetchone()[0] == 0
        assert (
            conn.execute("SELECT value FROM settings WHERE key='usage_state'").fetchone()[0] == "{}"
        )


def test_one_direction_reset_does_not_recount_other_direction(control):
    device = control.add("partial-reset", "desktop")["id"]
    store = UsageStore(control.db)
    store.record(sample(device, 6000, 100))
    changed = sample(device, 6005, 10)
    changed["connections"][0]["download"] = 240
    store.record(changed)
    usage = store.device(device, now=6005)
    assert usage["upload_bytes"] == 110 and usage["download_bytes"] == 240
    bucket = query(control, device)["buckets"][0]
    assert bucket["unallocated_upload_bytes"] == 110
    assert bucket["unallocated_download_bytes"] == 240
    assert bucket["last_used_at"] is None  # Snapshot/reset bytes have no precise traffic time.


def test_query_enforces_expiry_even_when_collection_is_stopped(control):
    device = control.add("expiry", "desktop")["id"]
    UsageStore(control.db).record(sample(device, 6000, 100))
    result = query(control, device, now=6000 + 31 * 86400)
    assert result["buckets"][0]["status"] == "expired"
    assert result["buckets"][0]["upload_bytes"] is None
