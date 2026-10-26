"""Software evidence tests: synthetic records and private temporary stores only."""

import hashlib
import json
import sys
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from wayvia.evidence import context
from wayvia.telemetry.collector import RollingCollector, trigger_channel
from wayvia.telemetry.records import (
    SOURCES,
    EvidenceError,
    Policy,
    canonical,
    digest,
    project,
    stamp,
)
from wayvia.telemetry.report import export_bundle, timeline, verify_bundle
from wayvia.telemetry.sources import command, interface_row, journal_events, tcp_rows
from wayvia.telemetry.store import DiagnosticStore

BASE = datetime(2026, 10, 26, tzinfo=UTC).timestamp()
REVISION = "a" * 64


def store(tmp_path, role="ingress", **policy):
    return DiagnosticStore(
        tmp_path / role,
        {
            "role": role,
            "revision": REVISION,
            "generation": ("b" if role == "ingress" else "c") * 32,
            "run_id": role,
            "path_id": "test-pair",
        },
        Policy(**policy),
    )


def sample(target, offset, **changes):
    row = {
        **context(
            role=target.metadata["role"],
            revision=REVISION,
            generation=target.metadata["generation"],
            run_id=target.metadata["run_id"],
            observed_at=stamp(BASE + offset),
        ),
        "event_id": digest([target.metadata["role"], offset]),
        "type": "sample",
        "sources": dict.fromkeys(SOURCES, "ok"),
        "tcp": [{"id": "e" * 16, "rtt_ms": 20, "send_q_bytes": 42}],
        "host": {"pressure": {}},
        "collector": {"cpu_ms": 1, "duration_ms": 2, "rss_bytes": 4096},
    }
    row.update(changes)
    return row


def event(target, offset, kind="probe_failed", **changes):
    return {
        **sample(target, offset),
        "type": "event",
        "event_id": digest([kind, offset, target.metadata["role"]]),
        "kind": kind,
        "source": "agent",
        "layer": "egress",
        "status": "failed",
        **changes,
    }


def test_both_roles_pin_before_and_after_single_role_failure_and_survive_rotation(tmp_path):
    ingress = store(tmp_path, buffer_seconds=300, retention_seconds=600)
    exit_store = store(tmp_path, "exit", buffer_seconds=300, retention_seconds=600)
    for offset in range(0, 305, 5):
        for target in (ingress, exit_store):
            target.append(sample(target, offset))
    incident = ingress.append(event(ingress, 300))
    exit_store.import_trigger(ingress.triggers()[0], now=BASE + 300)
    ingress.append(event(ingress, 320, "recovery_completed", status="healthy"))
    exit_store.import_trigger(ingress.triggers()[0], now=BASE + 320)
    for offset in range(305, 905, 5):
        for target in (ingress, exit_store):
            target.append(sample(target, offset))
    report = timeline(
        [ingress.path, exit_store.path],
        incident_id=incident,
        timezone="America/Los_Angeles",
        now=BASE + 900,
    )
    assert report["window"] == {"start": stamp(BASE + 180), "end": stamp(BASE + 440)}
    for role in ("ingress", "exit"):
        assert report["coverage"][role]["fraction"] == 1
        assert report["coverage"][role]["incident_pinned"]
        assert report["coverage"][role]["clock_alignment"] == "unknown"
    assert all(r["observed_at_local"].endswith("-07:00") for r in report["events"])
    assert {r["kind"] for r in report["events"]} == {"probe_failed", "recovery_completed"}
    ingress.prune(BASE + 2000)
    with pytest.raises(EvidenceError, match="operation failed"):
        timeline([ingress.path], incident_id=incident, now=BASE + 2000)


def test_missing_peer_and_holes_remain_explicit(tmp_path):
    target = store(tmp_path)
    target.append(sample(target, 0))
    target.append(sample(target, 20, sources={"tcp": "timeout"}))
    report = timeline([target.path], start=BASE, end=BASE + 30, now=BASE + 40)
    assert report["coverage"]["ingress"]["missing_slots"] == 4
    assert report["coverage"]["ingress"]["source_ok_slots"]["tcp"] == 1
    assert not report["coverage"]["exit"]["source_available"]
    assert report["coverage"]["exit"]["fraction"] == 0


def test_pending_post_window_is_not_complete(tmp_path):
    target = store(tmp_path)
    incident = target.append(event(target, 0))
    report = timeline([target.path], incident_id=incident, now=BASE + 1)
    assert report["coverage"]["ingress"]["status"] == "pending"


def test_long_window_limit_splits_instead_of_shrinking_existing_evidence(tmp_path):
    target = store(tmp_path, pre_seconds=600, post_seconds=600)
    first = target.append(event(target, 1000))
    assert target.append(event(target, 1500)) == first
    assert target.append(event(target, 1600)) == first
    before = target.triggers()[0]
    assert target.append(event(target, 900), now=BASE + 1600) != first
    assert next(row for row in target.triggers() if row["incident_id"] == first) == before


def test_late_probe_start_moves_shared_window_earlier_without_changing_incident_id(tmp_path):
    first, second = store(tmp_path), store(tmp_path, "exit")
    incident = first.append(event(first, 300, "protection_requested"))
    original = first.triggers()[0]
    second.import_trigger(original, now=BASE + 300)
    assert first.append(event(first, 270), now=BASE + 300) == incident
    earlier = first.triggers()[0]
    second.import_trigger(earlier, now=BASE + 300)
    second.import_trigger(original, now=BASE + 301)  # Stale delivery cannot shrink it.
    assert second.triggers()[0]["start"] == stamp(BASE + 150)
    assert second.triggers()[0]["incident_id"] == incident


def test_trigger_scope_conflict_and_capacity_preserve_existing_evidence(tmp_path):
    first = store(tmp_path, max_incidents=1)
    second = store(tmp_path, "exit")
    incident = first.append(event(first, 100))
    trigger = first.triggers()[0]
    with pytest.raises(EvidenceError):
        second.import_trigger({**trigger, "path_id": "other"}, now=BASE + 100)
    second.import_trigger(trigger, now=BASE + 100)
    with pytest.raises(EvidenceError):
        second.import_trigger({**trigger, "origin_generation": "e" * 32}, now=BASE + 110)
    with pytest.raises(EvidenceError):
        first.append(event(first, 500))
    assert first.triggers()[0]["incident_id"] == incident


def test_rolling_dedup_rejects_reused_id_with_different_data(tmp_path):
    target = store(tmp_path)
    row = sample(target, 0)
    target.append(row)
    assert target.append(row) is None
    with pytest.raises(EvidenceError):
        target.append({**row, "tcp": []})


def test_database_page_capacity_is_bounded_and_pinned_evidence_survives(tmp_path):
    target = store(tmp_path, max_bytes=8388608)
    incident = target.append(event(target, 0))
    row = sample(target, 0, tcp=[{"id": "e" * 16, "rtt_ms": 20} for _ in range(100)])
    stopped = False
    for index in range(250):
        try:
            target.append({**row, "event_id": digest([index, "quota"])})
        except EvidenceError as exc:
            assert exc.code == "EVIDENCE_STORAGE_UNAVAILABLE"
            stopped = True
            break
    assert stopped
    assert target.path.stat().st_size < target.policy.max_bytes // 2
    assert target.triggers()[0]["incident_id"] == incident


@pytest.mark.parametrize("component", ["tcp", "interfaces", "host"])
def test_projection_strips_every_unrecognized_field_even_from_bad_shapes(tmp_path, component):
    target = store(tmp_path)
    row = sample(target, 0, password="SECRET", url="SECRET", response_body="SECRET")
    row[component] = "SECRET"
    assert "SECRET" not in json.dumps(project(row))


def test_client_evidence_requires_explicit_authorization_and_identity_source(tmp_path):
    target = store(tmp_path)
    row = event(
        target,
        0,
        "business_failure",
        source="client",
        device_id="d" * 32,
        identity_source="user_confirmed",
        client_version="1.2.3",
        raw_log="SECRET",
    )
    with pytest.raises(EvidenceError):
        target.append(row)
    target.append(row, client_authorized=True)
    report = timeline([target.path], start=BASE - 1, end=BASE + 1, device_id="d" * 32, now=BASE + 2)
    assert len(report["events"]) == 1 and report["samples"] == []
    assert report["events"][0]["identity_source"] == "user_confirmed"
    assert "SECRET" not in json.dumps(report)


def test_readonly_query_and_export_preserve_source_and_redact(tmp_path):
    target = store(tmp_path)
    target.append(sample(target, 0, password="SECRET", plan={"key": "SECRET"}))
    target.append(event(target, 0, attempts=[{"result": "HTTP_TIMEOUT", "stderr": "SECRET"}]))
    before = hashlib.sha256(target.path.read_bytes()).hexdigest()
    report = timeline([target.path], start=BASE - 5, end=BASE + 5, now=BASE + 5)
    output = tmp_path / "export" / "evidence.zip"
    export_bundle(report, output)
    assert output.stat().st_mode & 0o777 == 0o600
    assert verify_bundle(output, now=BASE + 6)["status"] == "verified"
    with zipfile.ZipFile(output) as archive:
        assert all(b"SECRET" not in archive.read(name) for name in archive.namelist())
    assert hashlib.sha256(target.path.read_bytes()).hexdigest() == before
    with pytest.raises(FileExistsError):
        export_bundle(report, output)
    with pytest.raises(EvidenceError):
        verify_bundle(output, now=BASE + 9999999)


def test_tampered_bundle_is_rejected_without_extraction(tmp_path):
    target = store(tmp_path)
    report = timeline([target.path], start=BASE, end=BASE + 5, now=BASE)
    original, tampered = tmp_path / "export" / "original.zip", tmp_path / "export" / "tampered.zip"
    export_bundle(report, original)
    with zipfile.ZipFile(original) as archive, zipfile.ZipFile(tampered, "w") as changed:
        for name in archive.namelist():
            changed.writestr(name, b"changed" if name == "events.jsonl" else archive.read(name))
    tampered.chmod(0o600)
    with pytest.raises(EvidenceError) as raised:
        verify_bundle(tampered, now=BASE)
    assert raised.value.code == "EVIDENCE_CHECKSUM_MISMATCH"


def test_unsafe_store_links_and_permissions_are_rejected(tmp_path):
    target = store(tmp_path)
    alias = target.path.parent / "alias.db"
    alias.symlink_to(target.path)
    with pytest.raises(EvidenceError):
        timeline([alias], start=BASE, end=BASE + 5)
    target.path.chmod(0o644)
    with pytest.raises(EvidenceError):
        timeline([target.path], start=BASE, end=BASE + 5)


def test_socket_projection_queues_windows_and_retransmits_without_addresses():
    raw = (
        "ESTAB 42 84 10.0.0.1:12345 192.0.2.1:443\n"
        "\t rtt:20.5/2.5 rto:200 retrans:0/7 snd_wnd:4096 rcv_space:65535\n"
    )
    rows = tcp_rows(raw, salt="run")
    assert rows[0]["recv_q_bytes"] == 42 and rows[0]["send_q_bytes"] == 84
    assert rows[0]["retrans_total"] == 7 and rows[0]["peer_receive_window_bytes"] == 4096
    assert rows[0].get("receive_window_bytes") is None
    assert "192.0.2.1" not in json.dumps(rows) and "10.0.0.1" not in json.dumps(rows)


def test_interface_reset_never_creates_negative_throughput():
    raw = json.dumps([{"ifindex": 3, "stats64": {"rx": {"bytes": 10}, "tx": {"bytes": 20}}}])
    row = interface_row(raw, {"ifindex": 3, "rx_bytes": 100, "tx_bytes": 200}, 5)
    assert row["counter_state"] == "reset"
    assert row["rx_bytes_per_second"] is None and row["tx_bytes_per_second"] is None


def test_sampler_reads_versions_and_metrics_without_active_requests_or_raw_output(monkeypatch):
    import wayvia.telemetry.sources as module

    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "ip":
            return "ok", json.dumps(
                [{"ifindex": 3, "stats64": {"rx": {"bytes": 100}, "tx": {"bytes": 200}}}]
            )
        replies = {
            "ss": "ESTAB 0 0 10.0.0.1:42 192.0.2.1:9443\n\t rtt:20/2\n",
            "timedatectl": "yes\n",
            "sing-box": "sing-box version 1.14.3\nSECRET",
            "openvpn": "OpenVPN 2.6.19",
            "v2raya": "2.5.10",
            "v2raya_core": "V2RAYA_CORE 2.5.10",
        }
        return "ok", replies[argv[0]]

    monkeypatch.setattr(module, "host_metrics", lambda old: ({"pressure": {}}, [1] * 8))
    sampler = module.Sampler(
        role="ingress", peer="192.0.2.1", port=9443, interface="eth0", run_id="test", runner=runner
    )
    metadata = {"role": "ingress", "revision": REVISION, "generation": "b" * 32}
    row = project(sampler.sample(metadata, budget=SimpleNamespace(remaining=lambda: 60)))
    assert row["component_versions"]["sing_box"] == "1.14.3"
    assert row["component_versions"]["v2raya_core"] == "2.5.10"
    assert row["clock_sync"] == "synchronized" and row["clock_offset_ms"] is None
    assert "SECRET" not in json.dumps(row) and "192.0.2.1" not in json.dumps(row)
    assert calls[0][-1] == ":9443"
    sampler.sample(metadata, budget=SimpleNamespace(remaining=lambda: 60))
    assert sum(argv[0] == "sing-box" for argv in calls) == 1
    assert all(
        argv[0] in {"ss", "ip", "timedatectl", "sing-box", "openvpn", "v2raya", "v2raya_core"}
        for argv in calls
    )


def test_journal_classifies_service_jobs_ssh_and_raw_probe_logs_separately():
    base = {
        "__REALTIME_TIMESTAMP": str(int(BASE * 1000000)),
        "__CURSOR": "cursor",
        "_BOOT_ID": "a" * 32,
    }
    stopped = journal_events(
        {
            **base,
            "_PID": "1",
            "UNIT": "wayvia-ingress.service",
            "MESSAGE_ID": "9d1aaa27d60140bd96365438aad20286",
            "MESSAGE": "SECRET",
        },
        "ingress",
    )
    assert stopped[0]["kind"] == "service_stopped"
    assert stopped[0]["revision"] is None
    ssh = journal_events(
        {**base, "_SYSTEMD_UNIT": "ssh.service", "MESSAGE": "SECRET authentication failure"},
        "ingress",
    )
    assert ssh[0]["kind"] == "management_ssh"
    assert (
        journal_events(
            {**base, "_SYSTEMD_UNIT": "wayvia-ingress.service", "MESSAGE": "TLS reset SECRET"},
            "ingress",
        )
        == []
    )
    assert "SECRET" not in json.dumps(stopped + ssh)


def test_command_limits_output_without_network_or_unbounded_capture():
    status, raw = command([sys.executable, "-c", "print('x'*100000)"], max_bytes=1000)
    assert status == "truncated" and raw == ""
    status, raw = command(["never-start"], budget=SimpleNamespace(remaining=lambda: 0))
    assert status == "timeout" and raw == ""


def test_trigger_file_channel_pins_both_roles(tmp_path):
    first, second = store(tmp_path), store(tmp_path, "exit")
    first.append(event(first, 10))
    inbox, outbox = tmp_path / "inbox", tmp_path / "outbox"
    inbox.mkdir(mode=0o700)
    outbox.mkdir(mode=0o700)
    assert trigger_channel(first, inbox, outbox, BASE + 10) == "ok"
    assert trigger_channel(second, outbox, inbox, BASE + 11) == "ok"
    assert second.triggers()[0]["incident_id"] == first.triggers()[0]["incident_id"]
    second.append(event(second, 20, "recovery_completed", status="healthy"))
    assert trigger_channel(second, outbox, inbox, BASE + 20) == "ok"
    assert trigger_channel(first, inbox, outbox, BASE + 21) == "ok"
    assert first.triggers()[0]["end"] == stamp(BASE + 140)


def test_collector_step_is_passive_and_keeps_journal_failure_unknown(tmp_path, monkeypatch):
    import wayvia.telemetry.collector as module

    target = store(tmp_path)
    root = tmp_path / "agent"
    root.mkdir(mode=0o700)
    generation = target.metadata["generation"]
    for name, value in [
        ("ingress-runtime.json", {"transaction_id": generation}),
        (
            generation + ".json",
            {"revision": REVISION, "plan": {"password": "SECRET"}, "evidence": []},
        ),
    ]:
        path = root / name
        path.write_bytes(canonical(value))
        path.chmod(0o600)
    run = SimpleNamespace(contract=target.metadata, run_id="ingress", remaining=lambda: 60)
    monkeypatch.setattr(module, "collect_journal", lambda **k: ([], "timeout"))
    sampler = SimpleNamespace(sample=lambda *a, **k: sample(target, 0))
    collector = RollingCollector(
        run, target, peer="192.0.2.1", port=443, interface="eth0", agent_root=root, sampler=sampler
    )
    assert collector.step() == "continue"
    assert "journal_since" not in target.progress()
    report = timeline([target.path], start=BASE, end=BASE + 5, now=BASE + 10)
    assert report["samples"][0]["sources"]["journal"] == "timeout"
    assert "SECRET" not in json.dumps(report)


def test_legacy_snapshot_is_stable_across_collector_iterations(tmp_path, monkeypatch):
    import wayvia.telemetry.collector as module

    target = store(tmp_path)
    root = tmp_path / "agent"
    root.mkdir(mode=0o700)
    generation = target.metadata["generation"]
    evidence = [
        {
            "layer": "egress",
            "status": "failed",
            "observed_at": stamp(BASE),
            "attempts": [{"result": "HTTP_TIMEOUT"}],
        }
    ]
    for name, value in [
        ("ingress-runtime.json", {"transaction_id": generation}),
        (generation + ".json", {"revision": REVISION, "evidence": evidence}),
    ]:
        path = root / name
        path.write_bytes(canonical(value))
        path.chmod(0o600)
    run = SimpleNamespace(contract=target.metadata, run_id="ingress", remaining=lambda: 60)
    monkeypatch.setattr(module, "collect_journal", lambda **k: ([], "unavailable"))
    offsets = iter([0, 5])
    sampler = SimpleNamespace(sample=lambda *a, **k: sample(target, next(offsets)))
    collector = RollingCollector(
        run, target, peer="192.0.2.1", port=443, interface="eth0", agent_root=root, sampler=sampler
    )
    assert collector.step() == "continue" and collector.step() == "continue"
    result = timeline([target.path], start=BASE, end=BASE + 10, now=BASE + 10)
    assert len(result["events"]) == 1
    assert result["events"][0]["software_version"] is None


@pytest.mark.parametrize("shape", [{"physical": "SECRET"}, {"physical": []}, {"physical": None}])
def test_nested_projection_does_not_trust_container_types(tmp_path, shape):
    target = store(tmp_path)
    result = project(sample(target, 0, interfaces=shape, host={"pressure": {"cpu": "SECRET"}}))
    assert "SECRET" not in json.dumps(result)


def test_collector_health_snapshot_keeps_original_monitor_time(tmp_path, monkeypatch):
    import wayvia.telemetry.collector as module

    target = store(tmp_path)
    root = tmp_path / "agent"
    root.mkdir(mode=0o700)
    generation = target.metadata["generation"]
    transaction = {
        "revision": REVISION,
        "state": "DEGRADED",
        "evidence": [],
        "monitored_at": stamp(BASE - 1),
        "monitor_valid_until": stamp(BASE + 90),
    }
    for name, value in [
        ("ingress-runtime.json", {"transaction_id": generation}),
        (generation + ".json", transaction),
    ]:
        path = root / name
        path.write_bytes(canonical(value))
        path.chmod(0o600)
    run = SimpleNamespace(contract=target.metadata, run_id="ingress", remaining=lambda: 60)
    monkeypatch.setattr(module, "collect_journal", lambda **k: ([], "timeout"))
    sample_rows = iter([sample(target, 0), sample(target, 5)])
    collector = RollingCollector(
        run,
        target,
        peer="192.0.2.1",
        port=443,
        interface="eth0",
        agent_root=root,
        sampler=SimpleNamespace(sample=lambda *a, **k: next(sample_rows)),
    )
    assert collector.step() == "continue"
    assert collector.step() == "continue"
    result = timeline([target.path], start=BASE - 2, end=BASE + 5, now=BASE + 10)
    health = [r for r in result["events"] if r["kind"] == "health_degraded"]
    assert len(health) == 1 and health[0]["observed_at"] == stamp(BASE - 1)
    assert health[0]["monotonic_ms"] is None
