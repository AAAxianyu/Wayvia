"""Owner-local CLI and bounded evidence flows; never starts a collector or network."""

import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from wayvia.agent.transactions import TransactionManager
from wayvia.cli import app
from wayvia.evidence import context
from wayvia.telemetry.records import SOURCES, canonical, digest, stamp
from wayvia.telemetry.sources import audit_events, journal_events
from wayvia.telemetry.store import DiagnosticStore


def create_store(tmp_path):
    metadata = {
        "role": "ingress",
        "revision": "a" * 64,
        "generation": "b" * 32,
        "path_id": "pair",
        "run_id": "local",
    }
    store = DiagnosticStore(tmp_path / "store", metadata)
    now = datetime.now(UTC).timestamp()
    row = {
        **context(
            role="ingress",
            revision=metadata["revision"],
            generation=metadata["generation"],
            observed_at=stamp(now),
        ),
        "event_id": digest(["sample", now]),
        "type": "sample",
        "sources": dict.fromkeys(SOURCES, "ok"),
    }
    store.append(row)
    return store, now, row


def test_cli_readonly_timeline_export_verify_and_source_checksums(tmp_path):
    store, now, _ = create_store(tmp_path)
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    runner = CliRunner()
    options = ["--source", str(store.path), "--since", stamp(now - 5), "--until", stamp(now + 5)]
    result = runner.invoke(
        app, ["diagnostics", "timeline", *options, "--timezone", "America/Los_Angeles"]
    )
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)["samples"]) == 1
    bundle = tmp_path / "private" / "evidence.zip"
    result = runner.invoke(app, ["diagnostics", "export", *options, "--output", str(bundle)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["diagnostics", "verify", str(bundle)])
    assert result.exit_code == 0 and json.loads(result.stdout)["status"] == "verified"
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before


def test_cli_client_ingest_is_explicit_and_redacted(tmp_path):
    store, now, row = create_store(tmp_path)
    path = tmp_path / "client.jsonl"
    row.update(
        type="event",
        event_id=digest(["client", now]),
        kind="client_observation",
        source="client",
        device_id="c" * 32,
        identity_source="user_confirmed",
        client_version="1.2.3",
        body="SECRET",
    )
    path.write_bytes(canonical(row) + b"\n")
    path.chmod(0o600)
    runner = CliRunner()
    args = ["diagnostics", "ingest", str(store.path), str(path)]
    result = runner.invoke(app, args)
    assert result.exit_code != 0 and "SECRET" not in result.output
    result = runner.invoke(app, args + ["--authorize-client-logs"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app,
        [
            "diagnostics",
            "timeline",
            "--source",
            str(store.path),
            "--since",
            stamp(now - 5),
            "--until",
            stamp(now + 5),
            "--device",
            "c" * 32,
        ],
    )
    report = json.loads(result.stdout)
    assert report["events"][0]["identity_source"] == "user_confirmed"
    assert "SECRET" not in result.output and report["samples"] == []


def test_deep_invalid_json_never_reaches_a_traceback_with_raw_values(tmp_path):
    store, _, _ = create_store(tmp_path)
    path = tmp_path / "invalid.jsonl"
    depth = min(sys.getrecursionlimit() + 100, 100000)
    path.write_text("[" * depth + '"SECRET_PRIVATE_BODY"' + "]" * depth)
    path.chmod(0o600)
    result = CliRunner().invoke(app, ["diagnostics", "ingest", str(store.path), str(path)])
    assert result.exit_code != 0
    assert json.loads(result.stdout)["code"] in {"EVIDENCE_INPUT_INVALID", "EVIDENCE_INVALID"}
    assert "SECRET_PRIVATE_BODY" not in result.output


@pytest.mark.parametrize(
    "flag,value",
    [("--role", "SECRET"), ("--layer", "SECRET"), ("--device", "SECRET"), ("--timezone", "SECRET")],
)
def test_invalid_filters_fail_without_echoing_values(tmp_path, flag, value):
    store, now, _ = create_store(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "diagnostics",
            "timeline",
            "--source",
            str(store.path),
            "--since",
            stamp(now - 5),
            "--until",
            stamp(now + 5),
            flag,
            value,
        ],
    )
    assert result.exit_code != 0 and "SECRET" not in result.output


def test_existing_device_and_subscription_audit_is_projected_without_mutation(tmp_path):
    path = tmp_path / "audit.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE audit(id INTEGER, action TEXT, object_id TEXT, created_at REAL, actor TEXT); "
        "CREATE TABLE subscriptions(id TEXT, device_id TEXT, token_hash TEXT);"
    )
    connection.execute(
        "INSERT INTO audit VALUES(1,?,?,?,?)", ("device.revoke", "c" * 32, 100, "SECRET")
    )
    connection.execute("INSERT INTO subscriptions VALUES(?,?,?)", ("d" * 32, "c" * 32, "SECRET"))
    connection.execute(
        "INSERT INTO audit VALUES(2,?,?,?,?)", ("subscription.revoke", "d" * 32, 100, "SECRET")
    )
    connection.commit()
    connection.close()
    path.chmod(0o600)
    before = path.read_bytes()
    events, status = audit_events(path, since=99, until=101, role="ingress")
    assert status == "ok" and len(events) == 2
    assert {r["device_id"] for r in events} == {"c" * 32}
    assert all(r["identity_source"] == "credential_only" for r in events)
    assert all(r["software_version"] is None for r in events)
    assert "SECRET" not in json.dumps(events) and path.read_bytes() == before


def test_control_fact_emission_is_redacted_and_logging_failure_cannot_change_control(
    capsys, monkeypatch
):
    tx = {
        "role": "ingress",
        "id": "a" * 32,
        "revision": "b" * 64,
        "plan": {"password": "SECRET"},
        "error": "SECRET",
    }
    TransactionManager._event(None, "protection_applied", tx)
    raw = capsys.readouterr().err
    row = json.loads(raw)
    assert row["kind"] == "protection_applied" and "SECRET" not in raw
    journal = {
        "__CURSOR": "cursor",
        "__REALTIME_TIMESTAMP": "100000000",
        "_SYSTEMD_UNIT": "wayvia-monitor.service",
        "MESSAGE": raw,
    }
    assert journal_events(journal, "ingress")[0]["kind"] == "protection_applied"
    import wayvia.agent.transactions as module

    class Unavailable:
        def write(self, value):
            raise OSError("SECRET disk error")

    monkeypatch.setattr(module, "sys", SimpleNamespace(stderr=Unavailable()))
    TransactionManager._event(None, "recovery_started", tx)  # Must not propagate.
