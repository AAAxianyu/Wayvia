import json
from types import SimpleNamespace

import pytest
from test_transactions import Executor, manager, plan

from wayvia.adapters.linux import LinuxExecutor
from wayvia.agent import transactions
from wayvia.agent.models import State
from wayvia.agent.recovery import compact


class CarrierExecutor(Executor):
    """Reports a carrier socket whose received bytes advance only while moving."""

    def __init__(self):
        super().__init__()
        self.received, self.moving, self.socket = 1000, False, "a>b"

    def carrier_counters(self):
        if self.moving:
            self.received += 500
        return {"socket": self.socket, "received": self.received, "acked": 0}


def failing(error):
    def verifier(executor, probes):
        return [
            {"layer": p.layer, "status": "failed", "error": error}
            if p.layer == "egress"
            else {"layer": p.layer, "status": "healthy"}
            for p in probes
        ]

    return verifier


def healthy(executor, probes):
    return [{"layer": p.layer, "status": "healthy"} for p in probes]


def active(tmp_path, executor=None):
    m = manager(tmp_path, executor or CarrierExecutor())
    tx = m.confirm(m.apply(plan())["id"])
    m.monitor(tx["id"])  # First sample establishes the carrier baseline.
    m.executor.events.clear()
    return m, tx


@pytest.fixture
def clock(monkeypatch):
    now = SimpleNamespace(value=1_800_000_000.0)
    monkeypatch.setattr(transactions.time, "time", lambda: now.value)
    return now


def test_timeouts_without_carrier_progress_protect_after_three_rounds(tmp_path, clock):
    m, tx = active(tmp_path)
    m.verifier = failing("HTTP_TIMEOUT")
    for round_ in (1, 2):
        result = m.monitor(tx["id"])
        assert result["state"] == State.ACTIVE
        assert result["soft_failures"] == round_
    assert ("protect", "ingress") not in m.executor.events
    assert m.monitor(tx["id"])["state"] == State.DEGRADED
    assert ("protect", "ingress") in m.executor.events


def test_busy_carrier_is_not_a_failure_until_the_ceiling(tmp_path, clock):
    m, tx = active(tmp_path)
    m.executor.moving = True
    m.verifier = failing("HTTP_TLS_TRANSPORT_FAILED")
    for _ in range(m.soft_progress_rounds - 1):
        assert m.monitor(tx["id"])["state"] == State.ACTIVE
    assert ("protect", "ingress") not in m.executor.events
    assert m.monitor(tx["id"])["state"] == State.DEGRADED


def test_changed_carrier_socket_is_unknown_progress(tmp_path, clock):
    m, tx = active(tmp_path)
    m.executor.moving = True
    m.verifier = failing("HTTP_TIMEOUT")
    m.executor.socket = "c>d"
    m.monitor(tx["id"])
    m.executor.socket = "e>f"
    m.monitor(tx["id"])
    m.executor.socket = "g>h"
    assert m.monitor(tx["id"])["state"] == State.DEGRADED


@pytest.mark.parametrize(
    "error", [None, "HTTP_TLS_FAILED", "HTTP_IDENTITY_MISMATCH", "VPN_IDENTITY_REJECTED"]
)
def test_identity_and_unclassified_failures_protect_immediately(tmp_path, clock, error):
    m, tx = active(tmp_path)
    m.executor.moving = True
    m.verifier = failing(error)
    assert m.monitor(tx["id"])["state"] == State.DEGRADED
    assert ("protect", "ingress") in m.executor.events


def test_any_hard_layer_overrides_soft_timeouts(tmp_path, clock):
    m, tx = active(tmp_path)
    m.executor.moving = True
    m.verifier = lambda e, probes: [
        {"layer": p.layer, "status": "failed", "error": "HTTP_TIMEOUT"}
        if p.layer == "egress"
        else {"layer": p.layer, "status": "failed"}
        if p.layer == "vpn"
        else {"layer": p.layer, "status": "healthy"}
        for p in probes
    ]
    assert m.monitor(tx["id"])["state"] == State.DEGRADED


def test_a_healthy_round_resets_the_soft_streak(tmp_path, clock):
    m, tx = active(tmp_path)
    m.verifier = failing("COMMAND_TIMEOUT")
    m.monitor(tx["id"])
    m.monitor(tx["id"])
    m.verifier = healthy
    assert m.monitor(tx["id"])["soft_failures"] is None
    m.verifier = failing("COMMAND_TIMEOUT")
    m.monitor(tx["id"])
    assert m.monitor(tx["id"])["state"] == State.ACTIVE


def test_rebuilds_back_off_within_the_window(tmp_path, clock):
    m, tx = active(tmp_path)
    m.verifier = failing(None)
    assert m.monitor(tx["id"])["state"] == State.DEGRADED
    m.verifier = healthy
    assert m.monitor(tx["id"])["state"] == State.ACTIVE  # First rebuild is immediate.
    m.verifier = failing(None)
    assert m.monitor(tx["id"])["state"] == State.DEGRADED
    m.verifier = healthy
    clock.value += 10
    deferred = m.monitor(tx["id"])
    assert deferred["state"] == State.DEGRADED and deferred["rebuild_after"]
    clock.value += 25
    assert m.monitor(tx["id"])["state"] == State.ACTIVE
    clock.value += m.rebuild_window_seconds
    m.verifier = failing(None)
    m.monitor(tx["id"])
    m.verifier = healthy
    assert m.monitor(tx["id"])["state"] == State.ACTIVE  # The window has expired.


def test_healthy_rounds_emit_a_heartbeat_but_congestion_every_round(tmp_path, clock, capsys):
    m, tx = active(tmp_path)
    capsys.readouterr()

    def kinds():
        return [json.loads(line)["kind"] for line in capsys.readouterr().err.splitlines()]

    m.monitor(tx["id"])
    assert kinds() == []
    clock.value += m.health_heartbeat_seconds
    m.monitor(tx["id"])
    assert kinds() == ["health_healthy"]
    m.verifier = failing("HTTP_TIMEOUT")
    m.monitor(tx["id"])
    m.monitor(tx["id"])
    assert kinds() == ["health_congested", "health_congested"]
    m.verifier = healthy
    m.monitor(tx["id"])
    assert kinds() == ["health_healthy"]


def test_monitor_output_is_compact_only_for_healthy_fast_rounds():
    evidence = {
        "layer": "egress",
        "status": "healthy",
        "duration_ms": 400,
        "observed_at": "2026-11-02T00:00:00+00:00",
        "event_id": "a" * 32,
        "attempts": [{"curl_code": 0}],
        "boot_id": "x",
    }
    row = {"id": "1", "role": "ingress", "state": "ACTIVE", "error": None, "evidence": [evidence]}
    assert compact(row) == {
        "id": "1",
        "role": "ingress",
        "state": "ACTIVE",
        "evidence": [
            {k: evidence[k] for k in ("layer", "status", "duration_ms")}
            | {"observed_at": evidence["observed_at"], "event_id": evidence["event_id"]}
        ],
    }
    slow = {**row, "evidence": [{**evidence, "duration_ms": 2500}]}
    assert compact(slow) is slow
    congested = {**row, "soft_failures": 1}
    assert compact(congested) is congested
    degraded = {**row, "state": "DEGRADED"}
    assert compact(degraded) is degraded


class Runner:
    def __init__(self, stdout):
        self.stdout, self.argv = stdout, None

    def run(self, argv, **kwargs):
        self.argv = argv
        return SimpleNamespace(stdout=self.stdout, returncode=0)


def write_role(tmp_path, role, server="203.0.113.7"):
    (tmp_path / "manifest.json").write_text(json.dumps({"role": role}))
    (tmp_path / "carrier.json").write_text(
        json.dumps({"outbounds": [{"tag": "japan-carrier", "server": server, "server_port": 443}]})
    )
    (tmp_path / "exit.json").write_text(
        json.dumps({"inbounds": [{"tag": "carrier", "listen_port": 443}]})
    )


SS = (
    "0      0      10.0.0.5:60596 203.0.113.7:443\n"
    "\t cubic rtt:49/6 bytes_sent:10 bytes_acked:900 bytes_received:5000 segs_out:1\n"
    "0      0      10.0.0.5:60600 203.0.113.7:443\n"
    "\t cubic bytes_acked:10 bytes_received:20\n"
)


def test_ingress_carrier_counters_select_the_long_lived_socket(tmp_path):
    write_role(tmp_path, "ingress")
    runner = Runner(SS)
    executor = LinuxExecutor(runner=runner, config_dir=tmp_path, enforce_environment=False)
    assert executor.carrier_counters() == {
        "socket": "10.0.0.5:60596>203.0.113.7:443",
        "received": 5000,
        "acked": 900,
    }
    assert runner.argv[-6:] == ["dport", "=", ":443", "and", "dst", "203.0.113.7"]


def test_exit_carrier_counters_filter_the_listener(tmp_path):
    write_role(tmp_path, "exit")
    runner = Runner("")
    executor = LinuxExecutor(runner=runner, config_dir=tmp_path, enforce_environment=False)
    assert executor.carrier_counters() is None
    assert runner.argv[-3:] == ["sport", "=", ":443"]


def test_hostname_carrier_is_not_guessed(tmp_path):
    write_role(tmp_path, "ingress", server="carrier.example.net")
    runner = Runner(SS)
    executor = LinuxExecutor(runner=runner, config_dir=tmp_path, enforce_environment=False)
    assert executor.carrier_counters() is None
    assert runner.argv is None


def test_monitor_restores_owned_tunnel_shaping_each_round(tmp_path, clock, capsys):
    m, tx = active(tmp_path)
    calls = []
    m.executor.ensure_shaping = lambda: calls.append(1) or len(calls) == 2
    m.monitor(tx["id"])
    m.monitor(tx["id"])
    assert len(calls) == 2
    kinds = [json.loads(line)["kind"] for line in capsys.readouterr().err.splitlines()]
    assert kinds.count("shaping_restored") == 1

    def broken():
        raise RuntimeError("tc unavailable")

    m.executor.ensure_shaping = broken
    assert m.monitor(tx["id"])["state"] == State.ACTIVE  # Never affects health.
