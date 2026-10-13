"""Ownership checks for the disposable lab never invoke Docker in software tests."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "scripts/transaction_lab.py"
SPEC = importlib.util.spec_from_file_location("wayvia_transaction_lab_test", SOURCE)
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)

PROBE_SPEC = importlib.util.spec_from_file_location(
    "wayvia_transaction_probe_test", SOURCE.parents[1] / "tests/lab/transaction_probe.py"
)
probe = importlib.util.module_from_spec(PROBE_SPEC)
PROBE_SPEC.loader.exec_module(probe)


@pytest.fixture
def readiness_clock(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        probe,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            sleep=lambda duration: clock.__setitem__(0, clock[0] + duration),
        ),
    )
    return clock


def test_agent_readiness_waits_for_socket_status_but_not_business_health(
    monkeypatch, readiness_clock
):
    unavailable = {"agent": {"status": "unknown", "code": "agent_unavailable"}}
    degraded = {"agent": [{"role": "ingress", "current": True, "state": "DEGRADED", "id": "a"}]}
    replies = iter([unavailable, degraded])
    calls = []

    def cli(*args, **kwargs):
        calls.append(args)
        return next(replies)

    monkeypatch.setattr(probe, "cli", cli)
    assert probe.wait_agent_ready() is degraded
    assert calls == [("status", "--json"), ("status", "--json")]
    assert readiness_clock[0] == 0.1


def test_empty_initial_agent_journal_is_ready(monkeypatch, readiness_clock):
    monkeypatch.setattr(probe, "cli", lambda *args, **kwargs: {"agent": []})
    assert probe.wait_agent_ready() == {"agent": []}
    assert readiness_clock[0] == 0


def test_agent_readiness_timeout_names_the_unavailable_observation(monkeypatch, readiness_clock):
    monkeypatch.setattr(
        probe,
        "cli",
        lambda *args, **kwargs: {"agent": {"status": "unknown", "code": "agent_unavailable"}},
    )
    with pytest.raises(RuntimeError, match="AGENT_READINESS_TIMEOUT.*agent_unavailable"):
        probe.wait_agent_ready(timeout=0.2)


@pytest.mark.parametrize(
    "rows", [None, ["bad-row"], {"status": "unknown", "code": "peer_rejected"}]
)
def test_agent_readiness_does_not_retry_invalid_or_unexpected_status(
    monkeypatch, readiness_clock, rows
):
    monkeypatch.setattr(probe, "cli", lambda *args, **kwargs: {"agent": rows})
    with pytest.raises(RuntimeError, match="AGENT_STATUS_(INVALID|UNKNOWN)"):
        probe.wait_agent_ready()
    assert readiness_clock[0] == 0


def test_current_generation_ignores_old_active_generation_during_confirmation(monkeypatch):
    rows = [
        {"id": "a", "role": "ingress", "state": "ACTIVE", "current": False},
        {"id": "b", "role": "ingress", "state": "AWAITING_CONFIRM", "current": True},
    ]
    monkeypatch.setattr(probe, "cli", lambda *args: {"agent": rows})
    assert probe.current_state("ingress") is rows[1]


@pytest.mark.parametrize(
    "rows, message",
    [
        (
            {"status": "unknown", "code": "agent_unavailable"},
            "AGENT_STATUS_UNKNOWN.*agent_unavailable",
        ),
        (["bad-row"], "AGENT_STATUS_INVALID"),
        ([], "CURRENT_GENERATION_INVALID.*matching_generations=0"),
        (
            [{"role": "ingress", "state": "ACTIVE", "current": True}] * 2,
            "CURRENT_GENERATION_INVALID.*matching_generations=2",
        ),
    ],
)
def test_current_generation_has_explicit_diagnostics(monkeypatch, rows, message):
    monkeypatch.setattr(probe, "cli", lambda *args: {"agent": rows})
    with pytest.raises(RuntimeError, match=message):
        probe.current_state("ingress")


def test_cleanup_continues_owned_resources_after_refusing_an_unowned_collision(monkeypatch):
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "inspect":
            owned = argv[-1] != lab.NAMES["ingress"]
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Config": {
                                "Labels": {
                                    "org.wayvia.disposable": "transaction" if owned else "other"
                                }
                            }
                        }
                    ]
                ),
            )
        if argv[1:3] == ["network", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([{"Labels": {"org.wayvia.disposable": "transaction"}}]),
            )
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(lab, "command", command)
    with pytest.raises(RuntimeError, match="unowned transaction container"):
        lab.clean()
    assert ["docker", "rm", "-f", lab.NAMES["ingress"]] not in calls
    assert ["docker", "rm", "-f", lab.NAMES["exit"]] in calls
    assert ["docker", "rm", "-f", lab.NAMES["target"]] in calls
    assert ["docker", "network", "rm", lab.NETWORK] in calls


def test_collision_preflight_does_not_mutate_docker_resources(monkeypatch):
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="[]")

    monkeypatch.setattr(lab, "command", command)
    with pytest.raises(RuntimeError, match="already exists"):
        lab.require_absent()
    assert calls and all(argv[2] == "inspect" for argv in calls)


def test_transaction_lab_uses_daemon_addresses_on_automatic_subnets(monkeypatch, tmp_path):
    calls = []
    addresses = {"ingress": "172.30.0.25", "exit": "172.30.0.17", "target": "172.30.0.9"}
    received = []

    def command(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "run":
            # Docker releases which reject static endpoints on automatic IPAM must work.
            assert "--ip" not in argv
        if argv[1] == "inspect":
            role = next(role for role, name in lab.NAMES.items() if name == argv[-1])
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Config": {"Labels": {"org.wayvia.disposable": "transaction"}},
                            "NetworkSettings": {
                                "Networks": {lab.NETWORK: {"IPAddress": addresses[role]}}
                            },
                        }
                    ]
                ),
            )
        if argv[-1] == "is-system-running":
            return SimpleNamespace(returncode=0, stdout="running\n")
        if argv[-1] == "/proc/1/comm":
            return SimpleNamespace(returncode=0, stdout="systemd\n")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(lab, "command", command)
    monkeypatch.setattr(lab, "require_absent", lambda: None)
    monkeypatch.setattr(lab, "clean", lambda: None)
    monkeypatch.setattr(lab, "inputs", lambda directory, actual: received.append(actual.copy()))
    monkeypatch.setattr(lab, "action", lambda *args: {})
    assert lab.run(tmp_path) == 0
    assert received == [addresses]
    assert len([argv for argv in calls if argv[1] == "run"]) == 3


@pytest.mark.parametrize("address", ["", "::1", "0.0.0.0", "127.0.0.1"])
def test_transaction_container_address_rejects_unusable_endpoints(monkeypatch, address):
    data = [
        {
            "Config": {"Labels": {"org.wayvia.disposable": "transaction"}},
            "NetworkSettings": {"Networks": {lab.NETWORK: {"IPAddress": address}}},
        }
    ]
    monkeypatch.setattr(lab, "command", lambda *args: SimpleNamespace(stdout=json.dumps(data)))
    with pytest.raises((ValueError, RuntimeError)):
        lab.container_address("ingress")


def test_transaction_container_address_rejects_unowned_container(monkeypatch):
    data = [{"Config": {"Labels": {"org.wayvia.disposable": "other"}}}]
    monkeypatch.setattr(lab, "command", lambda *args: SimpleNamespace(stdout=json.dumps(data)))
    with pytest.raises(RuntimeError, match="unowned transaction container"):
        lab.container_address("ingress")
