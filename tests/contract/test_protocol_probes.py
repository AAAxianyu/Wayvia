import json
import time
from types import SimpleNamespace

import pytest
from test_topology import artifacts, probes

from wayvia.adapters.base import AdapterError, CommandResult
from wayvia.adapters.linux import LinuxExecutor, unit_content
from wayvia.agent.models import Action, Probe
from wayvia.agent.probes import verify
from wayvia.agent.validation import validate_probes


def test_protocol_result_is_used_and_bound_to_fresh_observation(tmp_path):
    calls = []

    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        return CommandResult(0, json.dumps({"healthy": True, "check": "vpn_server_identity"}))

    executor = SimpleNamespace(config_dir=tmp_path, _cmd=command)
    selected = Probe(layer="carrier", kind="vpn_handshake", target="127.0.0.1", port=1194)
    evidence = verify(executor, [selected])
    assert evidence[0]["status"] == "healthy"
    assert evidence[0]["check"] == "vpn_server_identity"
    assert evidence[0]["observed_at"] and evidence[0]["duration_ms"] >= 0
    assert evidence[0]["scope"] == "component_readiness"
    assert "wayvia.agent.probe_runtime" in calls[0][0]
    assert json.loads(calls[0][1]["input_text"])["kind"] == "vpn_handshake"


@pytest.mark.parametrize("output", ['{"healthy": false}', "not-json", '{"healthy": "true"}'])
def test_failed_protocol_is_never_healthy(tmp_path, output):
    executor = SimpleNamespace(config_dir=tmp_path, _cmd=lambda *a, **k: CommandResult(0, output))
    evidence = verify(executor, [Probe(layer="vpn", kind="vpn_session", target="10.77.0.1")])
    assert evidence[0]["status"] == "failed"


def test_whole_probe_budget_is_enforced_before_and_after_command(tmp_path):
    calls = []

    def slow(*args, **kwargs):
        calls.append(kwargs["timeout"])
        time.sleep(0.025)
        return CommandResult(0, "active")

    executor = SimpleNamespace(config_dir=tmp_path, _cmd=slow)
    evidence = verify(
        executor,
        [Probe(layer="return", kind="service", target="wayvia-ingress.service")] * 2,
        deadline_monotonic=time.monotonic() + 0.015,
    )
    assert all(row["error"] == "PROBE_DEADLINE" for row in evidence)
    assert len(calls) == 1 and calls[0] <= 0.015


def test_probe_user_is_private_stable_and_cannot_be_device(tmp_path):
    executor = LinuxExecutor(config_dir=tmp_path / "etc", state_dir=tmp_path / "state")
    action = Action(
        kind="install_config", resource="ingress.json", content=artifacts()["ingress.json"]
    )
    prepared = executor.prepare_ingress(action)
    again = executor.prepare_ingress(prepared)
    assert prepared == again
    assert "wayvia-internal-probe" not in action.content
    users = json.loads(prepared.content)["inbounds"][0]["users"]
    assert len(users) == 2
    assert (tmp_path / "state/probe-password").stat().st_mode & 0o777 == 0o600
    with pytest.raises(AdapterError, match="credential"):
        executor.validate_devices([{"id": "wayvia-internal-probe", "password": "x" * 32}])


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "tcp"},
        {"target": "192.0.2.99"},
        {"port": 8443},
        {"url": "https://wrong.example/probe"},
        {"expected": "wrong"},
    ],
)
def test_access_probe_cannot_substitute_port_or_other_business_path(changes):
    from wayvia.agent.planner import build_plan

    items = list(probes())
    items[0] = items[0].model_copy(update=changes)
    plan = build_plan("ingress", "1", artifacts(), "baseline", probes=tuple(items))
    with pytest.raises(AdapterError):
        validate_probes(plan)


def test_live_vpn_state_uses_private_management_socket():
    for role in ("client", "server"):
        unit = unit_content(f"wayvia-vpn-{role}.service")
        assert f"--management /run/wayvia/vpn-{role}.sock unix" in unit
        assert "UMask=0077" in unit


@pytest.mark.parametrize("layer,kind", [("access", "tcp"), ("carrier", "tcp"), ("vpn", "route")])
def test_legacy_journal_cannot_reacquire_healthy_with_port_or_route_probe(tmp_path, layer, kind):
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy connectivity must not run")

    executor = SimpleNamespace(config_dir=tmp_path, _cmd=forbidden)
    evidence = verify(executor, [Probe(layer=layer, kind=kind, target="127.0.0.1", port=443)])
    assert evidence[0]["status"] == "failed"
    assert evidence[0]["error"] == "PROBE_UPGRADE_REQUIRED"
