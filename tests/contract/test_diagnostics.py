from datetime import UTC, datetime, timedelta

import httpx
import pytest

from wayvia.adapters.base import AdapterError
from wayvia.adapters.v2raya import V2rayAAdapter
from wayvia.agent.models import LAYERS
from wayvia.services.diagnostics import dataplane_diagnostic, diagnose

NOW = datetime(2026, 10, 13, 0, 0, tzinfo=UTC)


def active(**changes):
    return {
        "id": "transaction-one",
        "role": "ingress",
        "state": "ACTIVE",
        "current": True,
        "revision": "revision-one",
        "generation": "transaction-one",
        "verification_scope": "datapath",
        "monitored_at": NOW.isoformat(),
        "monitor_valid_until": (NOW + timedelta(seconds=90)).isoformat(),
        "updated_at": "2026-10-12T12:00:00Z",
        "evidence": [
            {
                "layer": layer,
                "status": "healthy",
                "revision": "revision-one",
                "scope": "datapath",
                "generation": "transaction-one",
                "observed_at": NOW.isoformat(),
                "valid_until": (NOW + timedelta(seconds=90)).isoformat(),
                "duration_ms": 12,
            }
            for layer in sorted(LAYERS)
        ],
        **changes,
    }


@pytest.mark.parametrize(
    "transactions,expected",
    [
        ([], "unknown"),
        ([active(state="AWAITING_CONFIRM")], "unknown"),
        ([active(state="DEGRADED")], "failed"),
        ([active(state="RECOVERY_REQUIRED")], "failed"),
        ([active(evidence=[])], "unknown"),
        ([active(evidence=[{"layer": "vpn", "status": "healthy"}])], "unknown"),
        (
            [active(evidence=[{"layer": layer, "status": "unknown"} for layer in sorted(LAYERS)])],
            "unknown",
        ),
        ([active()], "healthy"),
        ([active(state="SUPERSEDED"), active(state="ROLLED_BACK")], "unknown"),
        (
            [
                active(),
                active(id="pending", state="AWAITING_CONFIRM", updated_at="2026-10-12T11:00:00Z"),
            ],
            "unknown",
        ),
    ],
)
def test_dataplane_requires_explicit_active_and_complete_path_evidence(transactions, expected):
    assert dataplane_diagnostic(transactions, now=NOW.timestamp())["status"] == expected


@pytest.mark.parametrize(
    "mutation,error",
    [
        (
            lambda tx: tx["evidence"][0].update(
                observed_at="2020-01-01T00:00:00Z", valid_until="2020-01-01T00:01:30Z"
            ),
            "path_evidence_stale",
        ),
        (
            lambda tx: tx["evidence"][0].update(generation="old-process"),
            "path_evidence_generation_mismatch",
        ),
        (
            lambda tx: tx["evidence"][0].update(revision="old-config"),
            "path_evidence_generation_mismatch",
        ),
        (lambda tx: tx["evidence"][0].update(duration_ms=-1), "path_evidence_duration_invalid"),
        (
            lambda tx: tx["evidence"][0].update(duration_ms=10**400),
            "path_evidence_duration_invalid",
        ),
        (lambda tx: tx["evidence"][0].update(layer=[]), "path_evidence_incomplete"),
        (lambda tx: tx.update(simulated=True), "simulated_evidence"),
        (lambda tx: tx["evidence"][0].update(simulated=True), "simulated_evidence"),
        (lambda tx: tx.update(monitored_at=None), "monitor_stale"),
        (
            lambda tx: tx.update(monitor_valid_until=(NOW - timedelta(seconds=1)).isoformat()),
            "monitor_stale",
        ),
    ],
)
def test_dataplane_rejects_stale_unbound_or_unmonitored_observations(mutation, error):
    tx = active()
    mutation(tx)
    result = dataplane_diagnostic([tx], now=NOW.timestamp())
    assert result["status"] == "unknown"
    assert result["roles"][0]["error"] == error


def test_recent_transaction_update_does_not_refresh_expired_probe_evidence():
    tx = active(updated_at=(NOW + timedelta(days=100)).isoformat())
    result = dataplane_diagnostic([tx], now=(NOW + timedelta(seconds=91)).timestamp())
    assert result["status"] == "unknown"
    assert result["roles"][0]["error"] == "path_evidence_stale"


def test_exit_component_readiness_is_not_end_to_end_dataplane_health():
    tx = active(role="exit", verification_scope="component_readiness")
    for probe in tx["evidence"]:
        probe["scope"] = "component_readiness"
    result = dataplane_diagnostic([tx], now=NOW.timestamp())
    assert result["status"] == "ready"
    assert result["roles"][0]["verification_scope"] == "component_readiness"
    assert result["roles"][0]["monitor"]["status"] == "observed"


def test_inactive_old_generation_cannot_hide_current_pending_deployment():
    previous = active(current=False)
    current = active(state="AWAITING_CONFIRM")
    assert dataplane_diagnostic([previous, current], now=NOW.timestamp())["status"] == "unknown"


def test_connected_agent_does_not_make_empty_dataplane_healthy(monkeypatch):
    import wayvia.services.diagnostics as module
    from wayvia.agent.client import AgentClient

    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.SingBoxAdapter, "inspect", lambda self: {"supported": True})
    monkeypatch.setattr(module.OpenVPNAdapter, "inspect", lambda self: {"supported": True})
    monkeypatch.setattr(module.V2rayAAdapter, "validate", lambda self: {"coreVersionValid": True})
    monkeypatch.setattr(AgentClient, "request", lambda self, request: [])
    result = diagnose()
    assert (
        next(layer for layer in result["layers"] if layer["layer"] == "agent")["status"]
        == "healthy"
    )
    assert (
        next(layer for layer in result["layers"] if layer["layer"] == "dataplane")["status"]
        == "unknown"
    )
    assert not result["healthy"]


@pytest.mark.parametrize(
    "reply",
    [
        None,
        [],
        "invalid",
        {"code": "SUCCESS", "data": None},
        {"code": "SUCCESS", "data": []},
        {"code": "SUCCESS", "data": {"coreVersionValid": True}},
        {"code": "SUCCESS", "data": {"version": "2.5.10", "coreVersionValid": "true"}},
    ],
)
def test_v2raya_malformed_version_reply_has_explicit_adapter_error(reply):
    client = httpx.Client(
        base_url="http://127.0.0.1:2017",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=reply)),
    )
    with pytest.raises(AdapterError):
        V2rayAAdapter(client=client).validate()


@pytest.mark.parametrize(
    "version,core", [("2.2.7", "2.2.7"), ("2.5.10", "2.5.9"), ("2.5.10", None)]
)
def test_v2raya_rejects_old_or_missing_core_version_even_if_valid_flag_true(version, core):
    client = httpx.Client(
        base_url="http://127.0.0.1:2017",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "code": "SUCCESS",
                    "data": {"version": version, "coreVersion": core, "coreVersionValid": True},
                },
            )
        ),
    )
    with pytest.raises(AdapterError) as error:
        V2rayAAdapter(client=client).validate()
    assert error.value.code == "VERSION_MISMATCH"
