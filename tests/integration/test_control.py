import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wayvia.api import create_app
from wayvia.config import WayviaConfig, example_config
from wayvia.errors import WayviaError
from wayvia.secrets import SecretStore
from wayvia.services.control import ControlService, SimulatedDataplane


@pytest.fixture
def control(tmp_path):
    return ControlService(
        tmp_path,
        WayviaConfig.model_validate(example_config()),
        mode="test",
        dataplane=SimulatedDataplane(),
    )


def test_independent_credentials_rotation_and_revocation(control):
    first = control.add("first", "android")
    second = control.add("second", "ios")
    password = control.dataplane.accepted[first["id"]]
    assert password != control.dataplane.accepted[second["id"]]
    sub = control.issue(first["id"], "sing-box")
    assert password in control.fetch(sub["token"], "sing-box")
    rotated = control.rotate(first["id"])
    assert rotated["credential_version"] == 2
    assert password not in control.fetch(sub["token"], "sing-box")
    assert control.revoke_device(first["id"])["status"] == "revoked"
    assert first["id"] not in control.dataplane.accepted
    with pytest.raises(WayviaError, match="unavailable"):
        control.fetch(sub["token"], "sing-box")
    assert control.device(second["id"])["status"] == "active"


def test_link_revoke_does_not_revoke_imported_credential(control):
    device = control.add("phone", "android")
    sub = control.issue(device["id"], "mihomo")
    other = control.issue(device["id"], "sing-box")
    password = control.dataplane.accepted[device["id"]]
    control.revoke_subscription(sub["id"])
    with pytest.raises(WayviaError):
        control.fetch(sub["token"], "mihomo")
    assert password in control.fetch(other["token"], "sing-box")
    assert control.dataplane.accepted[device["id"]] == password
    raw = control.db.path.read_bytes()
    assert sub["token"].encode() not in raw
    assert password.encode() not in raw


def test_failed_real_apply_keeps_pending_and_failed_revoke_degraded(tmp_path):
    class Failed:
        simulated = False

        def sync(self, devices, revision):
            raise OSError("unavailable")

    control = ControlService(
        tmp_path, WayviaConfig.model_validate(example_config()), dataplane=Failed()
    )
    device = control.add("pending", "android")
    assert device["status"] == "pending"
    with pytest.raises(WayviaError):
        control.export(device["id"], "sing-box")
    assert control.revoke_device(device["id"])["status"] == "degraded"


def test_production_rejects_simulation(tmp_path):
    with pytest.raises(WayviaError, match="real dataplane"):
        ControlService(
            tmp_path, WayviaConfig.model_validate(example_config()), dataplane=SimulatedDataplane()
        )


def test_auth_idempotency_and_subscriptions(control):
    admin = "a" * 48
    client = TestClient(create_app(control, admin))
    assert client.get("/api/v1/devices").status_code == 401
    assert client.get("/healthz").json()["dataplane"] == "unknown"
    headers = {"Authorization": "Bearer " + admin, "Idempotency-Key": "phone"}
    payload = {"name": "phone", "platform": "android"}
    first = client.post("/api/v1/devices", json=payload, headers=headers)
    assert first.status_code == 200
    assert client.post("/api/v1/devices", json=payload, headers=headers).json() == first.json()
    assert len(control.devices()) == 1
    assert (
        client.post(
            "/api/v1/devices", json=payload | {"name": "different"}, headers=headers
        ).status_code
        == 409
    )
    headers["Idempotency-Key"] = "sub"
    payload = {"device_id": first.json()["id"], "format": "sing-box"}
    sub = client.post("/api/v1/subscriptions", json=payload, headers=headers)
    assert sub.status_code == 200
    assert client.post("/api/v1/subscriptions", json=payload, headers=headers).json() == sub.json()
    assert sub.json()["token"].encode() not in control.db.path.read_bytes()
    response = client.get(sub.json()["path"])
    assert response.headers["cache-control"] == "no-store, private"
    assert response.status_code == 200
    assert client.get(sub.json()["path"].replace("sing-box", "mihomo")).status_code == 401
    assert "password" not in first.text and "secret_ref" not in first.text


def test_public_app_cannot_reach_management(control):
    client = TestClient(create_app(control, "a" * 48, subscriptions_only=True))
    assert client.get("/api/v1/status").status_code == 404
    assert client.post("/api/v1/devices").status_code == 404


def test_secrets_reject_permissions_symlink_and_traversal(tmp_path):
    store = SecretStore(tmp_path / "secrets")
    store.write("good", "secret")
    assert store.read("good") == "secret"
    assert os.stat(store.root / "good").st_mode & 0o777 == 0o600
    (store.root / "good").chmod(0o644)
    with pytest.raises(WayviaError):
        store.read("good")
    (store.root / "link").symlink_to(Path("/etc/passwd"))
    with pytest.raises(WayviaError):
        store.read("link")
    with pytest.raises(WayviaError):
        store.read("../passwd")


def test_request_errors_redact_inputs(control):
    client = TestClient(create_app(control, "a" * 48))
    response = client.post(
        "/api/v1/devices",
        json={"name": "secret-payload", "platform": "android", "unknown": "sensitive"},
        headers={"Authorization": "Bearer " + "a" * 48},
    )
    assert response.status_code == 422
    assert "sensitive" not in response.text and "secret-payload" not in response.text
    assert response.json()["request_id"]
    assert json.loads(response.text)["code"] == "invalid_request"


def test_corrupt_credential_blocks_existing_ingress_and_preserves_pending(control):
    device = control.add("existing", "android")
    assert control.dataplane.accepted
    with control.db.connect() as connection:
        ref = connection.execute(
            "SELECT secret_ref FROM devices WHERE id=?", (device["id"],)
        ).fetchone()[0]
    (control.secrets.root / ref).unlink()
    result = control.reconcile()
    assert result["status"] == "unconfirmed"
    assert result["protection"]["status"] == "blocked"
    assert not control.dataplane.accepted
    assert control.device(device["id"])["status"] == "degraded"


def test_expired_subscription_is_denied(control):
    device = control.add("expiring", "android")
    sub = control.issue(device["id"], "sing-box")
    with control.db.connect() as connection:
        connection.execute("UPDATE subscriptions SET expires_at=0 WHERE id=?", (sub["id"],))
    with pytest.raises(WayviaError):
        control.fetch(sub["token"], "sing-box")
