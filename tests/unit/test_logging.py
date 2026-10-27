import json
import os
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from wayvia.observability.connections import MESSAGES, Projector, endpoint
from wayvia.observability.files import policy, update_policy
from wayvia.observability.http import nginx_event
from wayvia.observability.models import Event, LogError, event
from wayvia.observability.store import LogStore
from wayvia.observability.system import journal_row
from wayvia.observability.transport import Spool, accept


@pytest.fixture
def logs(tmp_path):
    store = LogStore(tmp_path / "logs", create=True)
    update_policy(store.root, {"desired_enabled": True, "min_free_bytes": 0}, 1)
    return store


def request(source="nginx", **values):
    return event(
        "ali",
        "ingress",
        source,
        "epoch",
        "request",
        request_id="a" * 32,
        route="subscription_download",
        status=values.pop("status", 200),
        **values,
    )


def connection(**values):
    return event(
        "ali",
        "ingress",
        "connections",
        "epoch",
        "connection",
        device_id="a" * 32,
        identity_source="credential_only",
        connection_id="one",
        core_epoch="core",
        **values,
    )


def test_disabled_default_persistent_policy(tmp_path):
    store = LogStore(tmp_path / "logs", create=True)
    assert not store.policy()["desired_enabled"]
    with pytest.raises(LogError):
        store.ingest([request()])
    update_policy(store.root, {"desired_enabled": True}, 1)
    update_policy(store.root, {"desired_enabled": False}, 2)
    assert not LogStore(store.root, create=True).policy()["desired_enabled"]
    with pytest.raises(LogError):
        update_policy(store.root, {"desired_enabled": True}, 2)


@pytest.mark.parametrize(
    "field,value",
    [
        ("body", "secret"),
        ("url", "https://token"),
        ("cookie", "secret"),
        ("target_host", "https://secret/path"),
        ("source_ip", "not-ip"),
        ("observed_at", float("nan")),
        ("code", "arbitrary secret"),
    ],
)
def test_closed_model_does_not_persist_raw_input(field, value):
    data = request().data() | {field: value}
    with pytest.raises((ValidationError, ValueError)):
        Event.model_validate(data)


@pytest.mark.parametrize("reverse", [False, True])
def test_edge_identity_merge_and_exactly_one_request(logs, reverse):
    edge = request(status=429, limit_rate="REJECTED", source_ip="192.0.2.1")
    api = request(
        "api", device_id="a" * 32, subscription_id="b" * 32, identity_source="credential_only"
    )
    items = [edge, api] if not reverse else [api, edge]
    for item in items * 2:
        logs.ingest([item])
    result = logs.query()
    assert result["total"] == result["limited"] == 1
    row = result["items"][0]
    assert row["device_id"] == "a" * 32 and row["identity_source"] == "credential_only"
    assert row["status"] == 429 and row["code"] == "rate_limit" and row["source_ip"] == "192.0.2.1"
    with logs.connect() as con:
        assert con.execute("SELECT SUM(count) FROM rollups").fetchone()[0] == 1


@pytest.mark.parametrize(
    "values,reason",
    [
        ({"status": 429}, "unknown_429"),
        ({"status": 429, "limit_concurrent": "REJECTED"}, "concurrency_limit"),
        ({"status": 503}, "upstream_error"),
        ({"status": 403}, "denied"),
        ({"status": 200}, "none"),
    ],
)
def test_reason(logs, values, reason):
    logs.ingest([request(**values)])
    assert logs.query()["items"][0]["code"] == reason


def test_connection_replay_out_of_order_close_and_device_isolation(logs):
    now = time.time() - 1
    a = connection(lifecycle="active", upload_bytes=100, observed_at=now)
    closed = connection(
        lifecycle="closed", closed_at=now + 0.1, upload_bytes=120, observed_at=now + 0.1
    )
    for row in [a, closed, a, closed]:
        logs.ingest([row])
    assert logs.query()["total"] == 1
    row = logs.query()["items"][0]
    assert row["lifecycle"] == "closed" and row["upload_bytes"] == 120
    assert logs.query({"device_id": "b" * 32})["total"] == 0
    with pytest.raises(LogError):
        logs.ingest([closed.model_copy(update={"device_id": "b" * 32})])


def test_retention_is_applied_at_query_before_cleanup(logs):
    at = time.time() - 2 * 86400
    logs.ingest([request(observed_at=at)])
    filters = {"since": at - 10, "until": at + 10}
    assert logs.query(filters)["total"] == 1
    update_policy(logs.root, {"request_days": 1}, 2)
    assert logs.query(filters)["total"] == 0
    logs.prune()
    with logs.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert con.execute("SELECT SUM(count) FROM rollups").fetchone()[0] == 1


def test_query_bounds_and_cursor_contract(logs):
    now = time.time()
    for i in range(3):
        logs.ingest(
            [request().model_copy(update={"request_id": f"{i:032x}", "observed_at": now - i - 1})]
        )
    filters = {"since": now - 10, "until": now}
    first = logs.query(filters, limit=1)
    second = logs.query(filters, cursor=first["next_cursor"], limit=1)
    assert first["items"][0]["request_id"] != second["items"][0]["request_id"]
    for args in [
        {"filters": {"since": 0, "until": time.time()}},
        {"filters": filters, "limit": 501},
        {"filters": filters | {"device_id": "b" * 32}, "cursor": first["next_cursor"]},
    ]:
        with pytest.raises(LogError):
            logs.query(**args)


def test_disk_floor_does_not_change_control_data(logs):
    update_policy(logs.root, {"min_free_bytes": 10**18}, 2)
    with pytest.raises(LogError, match="could not"):
        logs.ingest([request()])
    assert logs.query()["total"] == 0


def test_spool_ack_replay_and_durable_checkpoint(logs, tmp_path):
    spool = Spool(tmp_path / "spool", "jp", "exit")
    item = event(
        "jp", "exit", "system", "epoch", "ssh_failed", source_ip="192.0.2.5", account="demo"
    )
    batch = spool.append([item], checkpoint={"cursor": "safe-cursor"})
    ack = accept(logs, batch, "jp", "exit")
    assert accept(logs, batch, "jp", "exit") == ack
    assert logs.query({"kind": "ssh_failed"})["total"] == 1
    assert spool.offer(ack)["batch"] is None
    with pytest.raises(LogError):
        accept(logs, batch, "ali", "ingress")
    assert json.loads(spool.state.read_text())["cursor"] == "safe-cursor"


def test_spool_size_is_bounded(tmp_path):
    spool = Spool(tmp_path / "spool", "jp", "exit", max_bytes=2000)
    for _ in range(10):
        spool.append([event("jp", "exit", "system", "epoch", "heartbeat")])
    assert sum(p.stat().st_size for p in spool.root.glob("batch-*.json")) <= 2000
    assert spool.offer()["dropped_batches"] > 0


def test_native_projection_deltas_domain_and_probe_exclusion():
    p = Projector("ali", "123", {"a" * 32, "b" * 32})
    initial = MESSAGES["Events"](reset=True)
    for device, ident in [("a" * 32, "one"), ("b" * 32, "two"), ("internal-probe", "three")]:
        update = initial.events.add()
        update.connection.CopyFrom(
            MESSAGES["Connection"](
                id=ident,
                user=device,
                inbound="entry",
                source="[::1]:1234",
                destination="example.com:443",
                createdAt=1700000000000,
                network="tcp",
                uplinkTotal=10,
            )
        )
    rows = p.project(initial)
    assert len(rows) == 3 and rows[1].target_host == "example.com" and rows[2].device_id == "b" * 32
    delta = MESSAGES["Events"]()
    delta.events.add(id="one", type=1, uplinkDelta=5, downlinkDelta=20)
    row = p.project(delta)[0]
    assert (row.upload_bytes, row.download_bytes) == (15, 20)
    closed = MESSAGES["Events"]()
    closed.events.add(id="one", type=2, closedAt=1700000005000)
    assert p.project(closed)[0].lifecycle == "closed"
    assert endpoint("192.0.2.1:443") == {"ip": "192.0.2.1", "port": 443}
    assert endpoint("https://secret/path") == {}


def test_ssh_projection_never_keeps_raw_message():
    row = {
        "_PID": "123",
        "_SYSTEMD_UNIT": "ssh.service",
        "__REALTIME_TIMESTAMP": "1700000000000000",
        "MESSAGE": "Accepted publickey for admin from 192.0.2.1 port 1234 ssh2: SECRET_FINGERPRINT",
    }
    projected = journal_row(row, "ali", "ingress")
    assert projected.kind == "ssh_accepted" and projected.account == "admin"
    assert "SECRET" not in projected.model_dump_json()
    assert journal_row(row | {"_SYSTEMD_UNIT": "unrelated.service"}, "ali", "ingress") is None
    assert journal_row(row | {"MESSAGE": "raw secret body"}, "ali", "ingress") is None


def test_safe_nginx_schema():
    data = {
        "wayvia_log": 1,
        "request_id": "a" * 32,
        "at": str(time.time()),
        "ip": "192.0.2.1",
        "method": "GET",
        "route": "subscription_download",
        "format": "mihomo",
        "status": "429",
        "bytes": "100",
        "duration": "0.01",
        "upstream": "-",
        "rate": "REJECTED",
        "concurrent": "",
    }
    row = nginx_event(
        b"<190>wayvia: " + json.dumps(data, separators=(",", ":")).encode(), "ali", "ingress"
    )
    assert row.duration_ms == 10 and row.upstream_ms is None
    with pytest.raises(ValueError):
        nginx_event(
            json.dumps(data | {"uri": "/secret"}, separators=(",", ":")).encode(), "ali", "ingress"
        )


def test_symlinks_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    with pytest.raises(LogError):
        LogStore(alias, create=True)


def test_new_database_permissions(logs):
    assert logs.path.stat().st_mode & 0o777 == 0o600
    assert logs.root.stat().st_mode & 0o777 == 0o700
    assert policy(logs.root).desired_enabled
    assert os.getuid() == logs.path.stat().st_uid
    assert Path(logs.path).exists()


def test_retention_window_does_not_retain_a_lifetime_of_long_connection_bytes(logs):
    now = time.time() - 1
    old = connection(
        lifecycle="active",
        created_at=now - 8 * 86400,
        upload_bytes=1000,
        download_bytes=2000,
        observed_at=now,
    )
    logs.ingest([old], now=now)
    row = logs.query(now=now + 0.1)["items"][0]
    assert row["created_at"] is None and row["upload_bytes"] == 0
    assert "counter_window" not in row
    logs.ingest(
        [
            old.model_copy(
                update={"upload_bytes": 1300, "download_bytes": 2500, "observed_at": now + 0.2}
            )
        ],
        now=now + 0.2,
    )
    row = logs.query(now=now + 0.3)["items"][0]
    assert row["upload_bytes"] == 300 and row["download_bytes"] == 500
    assert logs.query(now=now + 30)["items"][0]["lifecycle"] == "unknown"


def test_replayed_old_heartbeat_is_not_fresh(logs):
    now = time.time()
    logs.ingest([event("jp", "exit", "system", "epoch", "heartbeat", observed_at=now - 120)])
    row = next(s for s in logs.sources() if s["id"] == "jp:system")
    assert row["state"] == "stale"


@pytest.mark.parametrize(
    "filters",
    [
        {"target": "https://SECRET/path"},
        {"status": [200]},
        {"source_ip": "' OR 1=1--"},
        {"kind": "SECRET"},
        {"until": float("inf")},
        {"device_id": "../secret"},
    ],
)
def test_invalid_filters_are_safe_errors(logs, filters):
    with pytest.raises(LogError) as error:
        logs.query(filters)
    assert error.value.code == "LOG_QUERY_INVALID"
    assert "SECRET" not in str(error.value)


def test_spool_new_epoch_does_not_get_stuck_on_old_ack(tmp_path):
    spool = Spool(tmp_path / "spool", "jp", "exit")
    batch = spool.append([event("jp", "exit", "system", "new", "heartbeat")])
    assert spool.offer({"sequence": 1, "digest": "old-digest", "epoch": "f" * 32})["batch"] == batch


def test_sqlite_capacity_is_hard_bounded(logs):
    logs.update_policy({"max_data_bytes": 1048576}, 2)
    failed = False
    for batch in range(30):
        rows = [
            request().model_copy(update={"request_id": f"{batch * 200 + i:032x}"})
            for i in range(200)
        ]
        try:
            logs.ingest(rows)
        except LogError:
            failed = True
            break
    assert failed and logs.path.stat().st_size <= 1048576
    assert logs.query()["total"] % 200 == 0  # No partially committed batch.


def test_forced_ssh_relay_rejects_other_commands(monkeypatch):
    from typer.testing import CliRunner

    from wayvia.cli import app

    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "cat /etc/passwd")
    result = CliRunner().invoke(app, ["logs", "relay-ssh"])
    assert result.exit_code != 0 and "LOG_RELAY_COMMAND" in result.stdout
    assert "passwd" not in result.stdout
