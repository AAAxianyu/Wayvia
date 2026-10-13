import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from wayvia.agent.models import LAYERS
from wayvia.api import create_app
from wayvia.config import WayviaConfig, example_config
from wayvia.errors import WayviaError
from wayvia.services.control import AgentDataplane, ControlService, SimulatedDataplane


@pytest.fixture
def config():
    data = example_config()
    data["rulesets"]["domestic-domains"] = {"source": "test", "version": "1", "domains": ["cn"]}
    return WayviaConfig.model_validate(data)


def confirmation(config, role):
    now = datetime.now(UTC)
    observed, until = now.isoformat(), (now + timedelta(seconds=90)).isoformat()
    generation = role + "-generation"
    scope = "datapath" if role == "ingress" else "component_readiness"
    return {
        "id": generation,
        "generation": generation,
        "role": role,
        "revision": config.revision,
        "state": "ACTIVE",
        "current": True,
        "verification_scope": scope,
        "monitored_at": observed,
        "monitor_valid_until": until,
        "evidence": [
            {
                "layer": layer,
                "status": "healthy",
                "revision": config.revision,
                "generation": generation,
                "scope": scope,
                "observed_at": observed,
                "valid_until": until,
                "duration_ms": 1,
            }
            for layer in sorted(LAYERS)
        ],
    }


class ObservedDataplane(SimulatedDataplane):
    """Explicit test transport: no claim to exercise a real kernel or core."""

    simulated = False

    def __init__(self, config):
        super().__init__()
        self.observations = [confirmation(config, "ingress")]

    def status(self):
        return self.observations


def test_unpublished_production_profile_and_subscription_are_rejected(tmp_path, config):
    service = ControlService(tmp_path, config, dataplane=ObservedDataplane(config))
    device = service.add("phone", "android")
    for operation in (
        lambda: service.export(device["id"], "sing-box"),
        lambda: service.issue(device["id"], "sing-box"),
    ):
        with pytest.raises(WayviaError, match="explicitly publish"):
            operation()


def test_publication_persists_confirmed_snapshot_across_failed_change_and_restart(tmp_path, config):
    dataplane = ObservedDataplane(config)
    service = ControlService(tmp_path, config, dataplane=dataplane)
    device = service.add("phone", "android")
    publication = service.publish({"agent": [confirmation(config, "exit")]})
    assert publication["published_revision"] == config.revision
    assert set(publication["confirmations"]) == {"exit", "ingress"}
    sub = service.issue(device["id"], "sing-box")
    changed = config.model_dump()
    changed["entry"].update(endpoint="new.example:8443", server_name="new.example")
    new_config = WayviaConfig.model_validate(changed)
    restarted = ControlService(
        tmp_path, new_config, dataplane=AgentDataplane(tmp_path / "absent.sock")
    )
    # The production renderer uses the prior durable publication even when a new
    # desired configuration is loaded and the agent is unavailable.
    old = json.loads(restarted.fetch(sub["token"], "sing-box"))
    assert old["outbounds"][0]["server"] == "entry.wayvia.example"
    assert old["outbounds"][0]["server_port"] == 443
    with pytest.raises(WayviaError):
        restarted.publish([confirmation(config, "exit")])
    assert restarted.publication()["desired_revision"] == new_config.revision
    assert restarted.publication()["published_revision"] == config.revision
    # Confirm the second revision on both roles before it can replace the snapshot.
    updated = ControlService(tmp_path, new_config, dataplane=ObservedDataplane(new_config))
    updated.publish([confirmation(new_config, "exit")])
    new = json.loads(updated.fetch(sub["token"], "sing-box"))
    assert new["outbounds"][0]["server_port"] == 8443


@pytest.mark.parametrize(
    "mutation",
    [
        lambda receipt: receipt.update(state="AWAITING_CONFIRM"),
        lambda receipt: receipt.update(current=False),
        lambda receipt: receipt.update(simulated=True),
        lambda receipt: receipt["evidence"][0].update(simulated=True),
        lambda receipt: receipt.update(revision="different"),
        lambda receipt: receipt.update(monitor_valid_until="2020-01-01T00:00:00Z"),
        lambda receipt: receipt["evidence"][0].update(generation="previous"),
        lambda receipt: receipt["evidence"][0].update(status="failed"),
        lambda receipt: receipt["evidence"][0].update(observed_at="2020-01-01T00:00:00Z"),
    ],
)
def test_publication_rejects_unconfirmed_stale_or_mismatched_exit_receipt(
    tmp_path, config, mutation
):
    service = ControlService(tmp_path, config, dataplane=ObservedDataplane(config))
    receipt = confirmation(config, "exit")
    mutation(receipt)
    with pytest.raises(WayviaError):
        service.publish([receipt])
    assert service.publication()["status"] == "unpublished"


def test_publication_api_is_admin_only_idempotent_and_excludes_snapshot_secrets(tmp_path, config):
    service = ControlService(tmp_path, config, dataplane=ObservedDataplane(config))
    client = TestClient(create_app(service, "a" * 48))
    payload = {"exit_status": [confirmation(config, "exit")]}
    assert client.post("/api/v1/config/publication", json=payload).status_code == 401
    headers = {"Authorization": "Bearer " + "a" * 48, "Idempotency-Key": "publish-one"}
    first = client.post("/api/v1/config/publication", json=payload, headers=headers)
    assert first.status_code == 200
    assert (
        client.post("/api/v1/config/publication", json=payload, headers=headers).json()
        == first.json()
    )
    assert "carrier-credential" not in first.text
    public = TestClient(create_app(service, "a" * 48, subscriptions_only=True))
    assert public.get("/api/v1/config/publication").status_code == 404


class Crash(BaseException):
    pass


def test_rotation_crash_after_secret_fsync_can_retry_without_reusing_orphan(
    tmp_path, config, monkeypatch
):
    service = ControlService(tmp_path, config, mode="test", dataplane=SimulatedDataplane())
    device = service.add("phone", "android")
    old_password = service.dataplane.accepted[device["id"]]
    original = service.secrets.write
    orphan = []

    def crash(ref, password):
        original(ref, password)
        orphan.append(ref)
        raise Crash()

    monkeypatch.setattr(service.secrets, "write", crash)
    with pytest.raises(Crash):
        service.rotate(device["id"])
    recovered = ControlService(tmp_path, config, mode="test", dataplane=SimulatedDataplane())
    recovered.reconcile()
    assert recovered.dataplane.accepted[device["id"]] == old_password
    assert recovered.rotate(device["id"])["credential_version"] == 2
    with recovered.db.connect() as connection:
        row = connection.execute(
            "SELECT secret_ref FROM devices WHERE id=?", (device["id"],)
        ).fetchone()
    assert row["secret_ref"] != orphan[0]
    assert recovered.dataplane.accepted[device["id"]] != old_password


@pytest.mark.parametrize("after_apply", [False, True])
def test_rotation_crash_after_db_commit_reconciles_same_committed_credential(
    tmp_path, config, monkeypatch, after_apply
):
    dataplane = SimulatedDataplane()
    service = ControlService(tmp_path, config, mode="test", dataplane=dataplane)
    device = service.add("phone", "android")
    old_password = dataplane.accepted[device["id"]]
    original_sync = dataplane.sync

    def interrupted(devices, revision):
        if after_apply:
            original_sync(devices, revision)
        raise Crash()

    monkeypatch.setattr(dataplane, "sync", interrupted)
    with pytest.raises(Crash):
        service.rotate(device["id"])
    pending = service.device(device["id"])
    assert pending["credential_version"] == 2 and pending["desired_action"] == "rotate"
    with service.db.connect() as connection:
        row = connection.execute(
            "SELECT secret_ref FROM devices WHERE id=?", (device["id"],)
        ).fetchone()
    committed_password = service.secrets.read(row["secret_ref"])
    recovered = ControlService(tmp_path, config, mode="test", dataplane=SimulatedDataplane())
    recovered.reconcile()
    assert recovered.device(device["id"])["status"] == "active"
    assert recovered.dataplane.accepted[device["id"]] == committed_password != old_password
    assert recovered.rotate(device["id"])["credential_version"] == 3
