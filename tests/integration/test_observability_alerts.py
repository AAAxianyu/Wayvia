"""Synthetic private stores and mock notifications only; no sampler or outbound requests."""

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from wayvia.cli import app
from wayvia.evidence import context, sanitize_context
from wayvia.telemetry.alerts import AlertConfig, AlertStore, Channel
from wayvia.telemetry.clock import tracking
from wayvia.telemetry.records import SOURCES, EvidenceError, canonical, stamp
from wayvia.telemetry.store import DiagnosticStore

NOW = 1800000000


def write(path, data):
    path.write_bytes(canonical(data))
    path.chmod(0o600)


def row(store, at, kind=None, **values):
    meta = store.metadata
    return {
        **context(
            role=meta["role"],
            revision=meta["revision"],
            generation=meta["generation"],
            run_id=meta["run_id"],
            observed_at=stamp(at),
        ),
        "type": "event" if kind else "sample",
        "kind": kind,
        "sources": dict.fromkeys(SOURCES, "ok"),
        "clock_sync": "synchronized",
        **values,
    }


@pytest.fixture
def setup(tmp_path):
    stores, sources = [], []
    for role in ("ingress", "exit"):
        directory = tmp_path / role
        directory.mkdir(mode=0o700)
        meta = {
            "role": role,
            "path_id": "pair",
            "revision": "a" * 64,
            "generation": ("b" if role == "ingress" else "c") * 32,
            "run_id": role,
        }
        store = DiagnosticStore(directory / "rolling", meta)
        write(
            directory / "run.json",
            {
                "schema_version": 1,
                "run_id": role,
                "created_at": stamp(NOW - 300),
                "contract": {**meta, "mode": "rolling_passive", "until": stamp(NOW + 600)},
            },
        )
        write(directory / "state.json", {"status": "running", "at": stamp(NOW - 300)})
        for at in range(NOW - 300, NOW + 1, 5):
            store.append(row(store, at), now=NOW)
        store.append(row(store, NOW - 5, "probe_healthy", status="healthy"), now=NOW)
        stores.append(store)
        sources.append({"role": role, "database": str(store.path), "run_directory": str(directory)})
    config = AlertConfig.model_validate(
        {"watches": [{"id": "pair", "path_id": "pair", "sources": sources, "min_free_bytes": 100}]}
    )
    state = AlertStore(tmp_path / "alerts" / "alerts.db")

    def disk(_):
        return SimpleNamespace(free=10000000)

    return config, stores, state, disk


def evaluate(setup, now=NOW):
    config, _, state, disk = setup
    return state.evaluate(config, now=now, disk_usage=disk)


def test_normal_quiet_no_source_mutation_and_unknown_clock_explicit(setup):
    _, stores, _, _ = setup
    before = [hashlib.sha256(s.path.read_bytes()).hexdigest() for s in stores]
    result = evaluate(setup)
    assert result["alerts"] == [] and result["outbox_pending"] == 0
    assert evaluate(setup)["outbox_pending"] == 0
    assert before == [hashlib.sha256(s.path.read_bytes()).hexdigest() for s in stores]


def test_degraded_cross_role_explicit_incident_dedup_recovery_and_unknown(setup):
    config, stores, state, disk = setup
    stores[0].append(row(stores[0], NOW - 2, "health_degraded"), now=NOW)
    trigger = stores[0].triggers()[0]
    stores[1].import_trigger(trigger, now=NOW)
    stores[1].append(row(stores[1], NOW - 1, "health_degraded"), now=NOW)
    first = evaluate(setup)
    health = [a for a in first["alerts"] if a["category"] == "path_health"]
    assert len(health) == 1 and health[0]["degraded_roles"] == ["exit", "ingress"]
    assert health[0]["incident_id"] == trigger["incident_id"]
    assert evaluate(setup)["outbox_pending"] == 1
    # Loss of sources creates integrity alert, never fabricates business recovery.
    missing = stores[0].path.with_suffix(".saved")
    stores[0].path.rename(missing)
    second = evaluate(setup, NOW + 1)
    assert [a for a in second["alerts"] if a["category"] == "path_health"][0]["status"] == "open"
    missing.rename(stores[0].path)
    stores[0].append(row(stores[0], NOW + 2, "recovery_completed"), now=NOW + 2)
    third = evaluate(setup, NOW + 2)
    assert [a for a in third["alerts"] if a["category"] == "path_health"][0]["degraded_roles"] == [
        "exit"
    ]
    stores[1].append(row(stores[1], NOW + 3, "recovery_completed"), now=NOW + 3)
    final = evaluate(setup, NOW + 3)
    assert [a for a in final["alerts"] if a["category"] == "path_health"][0]["status"] == "resolved"
    pending = final["outbox_pending"]
    assert evaluate(setup, NOW + 3)["outbox_pending"] == pending
    # Process recreation preserves deduplication.
    assert (
        AlertStore(state.path).evaluate(config, now=NOW + 3, disk_usage=disk)["outbox_pending"]
        == pending
    )


def test_stale_events_dont_open_new_current_outages(setup):
    _, stores, _, _ = setup
    stores[0].append(row(stores[0], NOW - 200, "health_degraded"), now=NOW)
    result = evaluate(setup)
    assert not [a for a in result["alerts"] if a["category"] == "path_health"]


def test_stop_and_deadline_are_quiet_but_interruption_alerts(setup):
    config, _, state, disk = setup
    source = config.watches[0].sources[0]
    write(source.run_directory / "state.json", {"status": "interrupted", "reason": "interrupted"})
    result = evaluate(setup)
    assert "ingress:collector_unexpected_stop" in result["alerts"][0]["causes"]
    count = result["outbox_pending"]
    for source in config.watches[0].sources:
        write(
            source.run_directory / "stop.json", {"reason": "user_stop", "requested_at": stamp(NOW)}
        )
    quiet = evaluate(setup, NOW + 1)
    assert quiet["outbox_pending"] == count
    assert quiet["alerts"][0]["status"] == "ended"
    assert state.evaluate(config, now=NOW + 700, disk_usage=disk)["outbox_pending"] == count


def test_disk_coverage_log_hole_staleness_and_expiry_are_integrity(setup):
    config, stores, state, _ = setup
    result = state.evaluate(config, now=NOW + 121, disk_usage=lambda _: SimpleNamespace(free=0))
    integrity = result["alerts"][0]
    assert integrity["category"] == "observation_integrity"
    for value in (
        "ingress:collector_heartbeat_stale",
        "ingress:local_evidence_disk_low",
        "ingress:sample_coverage_low",
        "ingress:source_gap_journal",
        "ingress:health_evidence_expired",
    ):
        assert value in integrity["causes"]
    assert integrity["coverage"]["ingress"]["missing_ranges"]
    assert "collector_loss_is_not_business_outage" in integrity["uncertainty"]
    stores[0].append(row(stores[0], NOW, "health_degraded"), now=NOW)
    with stores[0].write() as conn:
        conn.execute("UPDATE incidents SET expires=?", (NOW + 122,))
    state.evaluate(config, now=NOW + 121, disk_usage=lambda _: SimpleNamespace(free=0))
    expired = state.evaluate(config, now=NOW + 123, disk_usage=lambda _: SimpleNamespace(free=0))
    assert "incident_evidence_expired" in expired["alerts"][0]["causes"]
    # Pruning the diagnostic incident does not erase the already tracked expiry.
    stores[0].prune(NOW + 123)
    assert (
        "incident_evidence_expired"
        in state.evaluate(config, now=NOW + 124, disk_usage=lambda _: SimpleNamespace(free=0))[
            "alerts"
        ][0]["causes"]
    )


def test_source_binding_malformed_data_and_required_offset(setup):
    config, stores, state, disk = setup
    changed = config.model_dump(mode="json")
    changed["watches"][0]["require_clock_offset"] = True
    required = AlertConfig.model_validate(changed)
    result = state.evaluate(required, now=NOW, disk_usage=disk)
    assert "ingress:clock_offset_unavailable" in result["alerts"][0]["causes"]
    manifest = config.watches[0].sources[0].run_directory / "run.json"
    data = json.loads(manifest.read_text())
    data["contract"]["revision"] = "SECRET BODY"
    write(manifest, data)
    result = state.evaluate(required, now=NOW + 1, disk_usage=disk)
    assert "SECRET BODY" not in json.dumps(result)
    assert "ingress:evidence_source_unreadable" in result["alerts"][0]["causes"]
    with pytest.raises(EvidenceError, match="Diagnostic"):
        state.evaluate(config, now=NOW + 1, disk_usage=disk)


def test_clock_measures_and_cross_role_offset_spread(setup):
    config, stores, state, disk = setup
    for store, offset in zip(stores, (-200, 200), strict=True):
        store.append(
            row(
                store,
                NOW + 1,
                clock_offset_ms=offset,
                clock_offset_source="chrony_tracking",
                clock_offset_observed_at=stamp(NOW + 1),
            ),
            now=NOW + 1,
        )
    result = state.evaluate(config, now=NOW + 1, disk_usage=disk)
    assert "reported_ntp_offset_spread_excessive" in result["alerts"][0]["causes"]
    assert "ingress:clock_offset_excessive" in result["alerts"][0]["causes"]
    assert (
        sanitize_context({"clock_offset_ms": 3, "observed_at": stamp(NOW)})["clock_offset_ms"]
        is None
    )
    assert (
        sanitize_context(
            row(
                stores[0],
                NOW,
                clock_offset_ms=-4,
                clock_offset_source="chrony_tracking",
                clock_offset_observed_at=stamp(NOW - 6),
            )
        )["clock_offset_ms"]
        is None
    )


def test_chrony_readonly_projection_rejects_local_unsynced_and_stale():
    from datetime import UTC, datetime

    reference = datetime.fromtimestamp(NOW - 1, UTC).strftime("%a %b %d %H:%M:%S %Y")
    text = (
        "Reference ID : CB00710F (SECRET-NTP-NAME)\nStratum : 3\n"
        f"Ref time (UTC) : {reference}\n"
        "System time : 0.002 seconds slow of NTP time\nLeap status : Normal\n"
    )
    assert tracking(text, NOW)["clock_offset_ms"] == -2
    assert "SECRET" not in json.dumps(tracking(text, NOW))
    assert tracking(text.replace("CB00710F", "7F7F0101"), NOW) == {}
    assert tracking(text.replace("Normal", "Not synchronised"), NOW) == {}
    assert tracking(text, NOW + 400) == {}
    assert tracking(text.replace("0.002", "nan"), NOW) == {}


def test_delivery_explicit_private_idempotent_and_mock_https_retry(setup, tmp_path):
    config, stores, state, _ = setup
    stores[0].append(row(stores[0], NOW, "health_degraded"), now=NOW)
    evaluate(setup)
    channel = Channel(kind="directory", destination=str(tmp_path / "channel"))
    with pytest.raises(EvidenceError):
        state.deliver(channel, authorized=False, now=NOW)
    assert not (tmp_path / "channel").exists()
    # Failed HTTP attempts keep stable event IDs, do not retain response secrets or redirect.
    requests = []

    def fail(request):
        requests.append(request)
        return httpx.Response(
            302, headers={"Location": "https://private.invalid/token=SECRET"}, text="SECRET"
        )

    https = Channel(kind="https", destination="https://alerts.invalid/receiver")
    assert (
        state.deliver(https, authorized=True, now=NOW, transport=httpx.MockTransport(fail))[
            "failed"
        ]
        == 1
    )
    assert len(requests) == 1

    def success(request):
        assert request.headers["Idempotency-Key"] == requests[0].headers["Idempotency-Key"]
        return httpx.Response(204)

    assert (
        state.deliver(https, authorized=True, now=NOW + 1, transport=httpx.MockTransport(success))[
            "sent"
        ]
        == 1
    )
    assert state.deliver(channel, authorized=True, now=NOW + 2)["sent"] == 0
    assert "SECRET" not in state.path.read_bytes().decode(errors="ignore")


def test_private_directory_delivery_and_outbox_full_rollback(setup, tmp_path):
    _, stores, state, _ = setup
    stores[0].append(row(stores[0], NOW, "health_degraded"), now=NOW)
    evaluate(setup)
    channel = Channel(kind="directory", destination=str(tmp_path / "channel"))
    assert state.deliver(channel, authorized=True, now=NOW)["sent"] == 1
    output = list((tmp_path / "channel").glob("*.json"))[0]
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.parent.stat().st_mode & 0o777 == 0o700
    # Simulated crash after durable file, before delivery checkpoint: same file is reused.
    with state.connection() as conn:
        conn.execute("UPDATE outbox SET delivered=NULL")
    assert state.deliver(channel, authorized=True, now=NOW)["sent"] == 1
    assert len(list(output.parent.glob("*.json"))) == 1
    state.LIMIT = 1
    stores[0].append(row(stores[0], NOW + 1, "recovery_completed"), now=NOW + 1)
    with pytest.raises(EvidenceError) as error:
        evaluate(setup, NOW + 1)
    assert error.value.code == "ALERT_OUTBOX_FULL"
    assert state.report(now=NOW + 1)["alerts"][0]["status"] == "open"


def test_channel_validation_and_cli_watchdog_export(setup, tmp_path):
    _, _, state, _ = setup
    for url in (
        "http://alerts.invalid",
        "https://user:SECRET@alerts.invalid",
        "https://alerts.invalid/?token=SECRET",
    ):
        with pytest.raises(ValueError):
            Channel(kind="https", destination=url)
    evaluate(setup)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["alerts", "list", str(state.path), "--output", str(tmp_path / "export" / "alerts.json")],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "export" / "alerts.json").stat().st_mode & 0o777 == 0o600
    result = runner.invoke(app, ["alerts", "watchdog", str(state.path)])
    assert result.exit_code == 5  # Synthetic historical evaluation is stale today.
    assert json.loads(result.stdout)["business_impact"] == "unknown"


def test_brief_failure_and_recovery_between_evaluations_are_not_lost(setup):
    _, stores, _, _ = setup
    evaluate(setup)
    stores[0].append(row(stores[0], NOW + 1, "health_degraded"), now=NOW + 1)
    stores[0].append(row(stores[0], NOW + 2, "health_healthy"), now=NOW + 2)
    result = evaluate(setup, NOW + 3)
    health = [a for a in result["alerts"] if a["category"] == "path_health"]
    assert health[0]["status"] == "resolved" and result["outbox_pending"] == 2
    assert evaluate(setup, NOW + 3)["outbox_pending"] == 2


def test_old_healthy_checkpoint_prevents_late_failure_replay(setup):
    _, stores, _, _ = setup
    stores[0].append(row(stores[0], NOW, "health_healthy"), now=NOW)
    evaluate(setup)
    stores[0].append(row(stores[0], NOW - 1, "health_degraded"), now=NOW)
    assert not [a for a in evaluate(setup, NOW + 1)["alerts"] if a["category"] == "path_health"]


def test_expired_notifications_are_visible_not_replayed_and_explicitly_discarded(setup, tmp_path):
    _, stores, state, _ = setup
    stores[0].append(row(stores[0], NOW, "health_degraded"), now=NOW)
    evaluate(setup)
    now = NOW + state.RETENTION + 1
    result = state.deliver(
        Channel(kind="directory", destination=str(tmp_path / "channel")), authorized=True, now=now
    )
    assert result["status"] == "expired_pending" and result["expired_pending"] == 1
    assert not (tmp_path / "channel").exists()
    assert state.discard_expired(now=now)["count"] == 1
    assert state.report(now=now)["outbox_pending"] == 0


def test_old_evidence_without_offset_extension_remains_byte_identical(setup):
    _, stores, _, _ = setup
    from wayvia.telemetry.records import project

    original = project(row(stores[0], NOW))
    assert "clock_offset_source" not in original
    stores[0].append(original, now=NOW)
    stores[0].append(project(original), now=NOW)  # No changed-ID conflict on re-import.


def test_source_quota_rotation_usage_and_storage_end_reason(setup):
    config, stores, state, disk = setup
    source = config.watches[0].sources[0]
    write(
        source.run_directory / "state.json", {"status": "failed", "reason": "storage_unavailable"}
    )
    with stores[0].write() as conn:
        record = row(stores[0], NOW - 1000)
        conn.execute(
            "INSERT INTO records VALUES(?,?,?,?,?)",
            (record["event_id"], NOW - 1000, "ingress", "sample", canonical(record).decode()),
        )
        # Force a synthetic near-quota policy; no disk or real fault injection.
        meta = dict(stores[0].metadata)
        meta["policy"] = {**meta["policy"], "max_bytes": 2097153}
        conn.execute(
            "UPDATE metadata SET value=? WHERE key='policy'", (canonical(meta["policy"]).decode(),)
        )
    # Malformed policy must be unknown source, not a trustworthy quota measurement.
    result = evaluate(setup)
    assert "ingress:evidence_source_unreadable" in result["alerts"][0]["causes"]
    assert "ingress:evidence_persistence_failed" in result["alerts"][0]["causes"]
    with stores[0].write() as conn:
        conn.execute(
            "UPDATE metadata SET value=? WHERE key='policy'",
            (canonical(stores[0].policy.model_dump()).decode(),),
        )
    result = evaluate(setup, NOW + 1)
    assert "ingress:evidence_rotation_overdue" in result["alerts"][0]["causes"]
    changed = config.model_dump(mode="json")
    changed["watches"][0]["usage_database"] = str(source.run_directory / "absent-control.db")
    other = AlertStore(state.path.parent / "other.db")
    result = other.evaluate(AlertConfig.model_validate(changed), now=NOW + 1, disk_usage=disk)
    assert "usage_observation_unreadable" in result["alerts"][0]["causes"]


def test_private_config_and_delivery_contract_binding(setup, tmp_path):
    from wayvia.telemetry.alerts import load_config

    config, _, state, _ = setup
    evaluate(setup)
    state.validate_contract(config)
    config_path = tmp_path / "config.json"
    write(config_path, config.model_dump(mode="json"))
    assert load_config(config_path) == config
    config_path.chmod(0o644)
    with pytest.raises(EvidenceError):
        load_config(config_path)
    changed = config.model_dump(mode="json")
    changed["watches"][0]["id"] = "another"
    with pytest.raises(EvidenceError):
        state.validate_contract(AlertConfig.model_validate(changed))


def test_ongoing_degradation_keeps_one_episode_across_bounded_incident_windows(setup):
    config, stores, _, _ = setup
    source = config.watches[0].sources[0]
    manifest = json.loads((source.run_directory / "run.json").read_text())
    manifest["contract"]["until"] = stamp(NOW + 3600)
    write(
        source.run_directory / "run.json", manifest
    )  # Initial synthetic contract before evaluation.
    stores[0].append(row(stores[0], NOW, "health_degraded"), now=NOW)
    first = evaluate(setup)
    initial = [a for a in first["alerts"] if a["category"] == "path_health"][0]
    stores[0].append(row(stores[0], NOW + 2000, "health_degraded"), now=NOW + 2000)
    assert len(stores[0].triggers()) == 2
    result = evaluate(setup, NOW + 2000)
    health = [a for a in result["alerts"] if a["category"] == "path_health"]
    assert len(health) == 1 and health[0]["event_id"] == initial["event_id"]
    assert health[0]["last_health_evidence"]["ingress"]["observed_at"] == stamp(NOW + 2000)
    assert (
        health[0]["last_health_evidence"]["ingress"]["trigger_incident_id"]
        != initial["incident_id"]
    )
