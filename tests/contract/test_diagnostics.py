import httpx
import pytest

from wayvia.adapters.base import AdapterError
from wayvia.adapters.v2raya import V2rayAAdapter
from wayvia.agent.models import LAYERS
from wayvia.services.diagnostics import dataplane_diagnostic, diagnose


def active(**changes):
    return {
        "id": "transaction-one",
        "role": "ingress",
        "state": "ACTIVE",
        "updated_at": "2026-10-12T12:00:00Z",
        "evidence": [{"layer": layer, "status": "healthy"} for layer in sorted(LAYERS)],
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
    assert dataplane_diagnostic(transactions)["status"] == expected


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
