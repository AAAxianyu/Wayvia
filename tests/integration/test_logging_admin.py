import json
import time

import pytest
from fastapi.testclient import TestClient

from wayvia.admin.app import create_app as admin_app
from wayvia.admin.control import ControlGateway
from wayvia.admin.state import AdminState
from wayvia.api import create_app
from wayvia.config import WayviaConfig, example_config
from wayvia.observability.files import update_policy
from wayvia.observability.models import LogError, event
from wayvia.observability.store import LogStore
from wayvia.services.control import ControlService, SimulatedDataplane

ORIGIN = "http://127.0.0.1:8790"


@pytest.fixture
def setup(tmp_path):
    control = ControlService(
        tmp_path / "control",
        WayviaConfig.model_validate(example_config()),
        mode="test",
        dataplane=SimulatedDataplane(),
    )
    device = control.add("JXY", "android")
    sub = control.issue(device["id"], "mihomo")
    state = AdminState(tmp_path / "admin", create=True)
    logs = LogStore(tmp_path / "logs", create=True)
    update_policy(logs.root, {"desired_enabled": True, "min_free_bytes": 0}, 1)
    gateway = ControlGateway("a" * 48, client=TestClient(create_app(control, "a" * 48)))
    client = TestClient(admin_app(state, logs, gateway), base_url=ORIGIN)
    return control, device, sub, state, logs, client


def login(state, client):
    response = client.post(
        "/admin/session", json={"code": state.code()}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200, response.text
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf"]}


def test_subscription_bytes_headers_and_credentials_unchanged_when_logging_fails(
    setup, monkeypatch
):
    control, device, sub, state, logs, _ = setup
    before = TestClient(create_app(control, "a" * 48)).get(sub["path"])
    rows = []

    def broken(row):
        rows.append(row)
        raise OSError("SYNTHETIC_SECRET_MUST_NOT_ESCAPE")

    client = TestClient(create_app(control, "a" * 48, log_sink=broken))
    after = client.get(sub["path"])
    assert before.content == after.content and before.status_code == after.status_code == 200
    for key in before.headers:
        if key != "x-request-id":
            assert before.headers[key] == after.headers[key]
    assert rows[0]["device_id"] == device["id"] and rows[0]["subscription_id"] == sub["id"]
    assert sub["token"] not in json.dumps(rows)
    denied = client.get("/subscriptions/INVALID_SYNTHETIC_SECRET/mihomo")
    assert denied.status_code == 401 and not rows[-1].get("device_id")
    assert "SYNTHETIC_SECRET" not in denied.text
    control.revoke_subscription(sub["id"])
    assert client.get(sub["path"]).status_code == 401


def test_default_public_application_has_no_admin_or_log_routes(setup):
    control, _, sub, _, _, _ = setup
    client = TestClient(create_app(control, "a" * 48, subscriptions_only=True))
    for path in ["/admin", "/admin/api/overview", "/api/v1/devices"]:
        assert (
            client.get(path, headers={"Authorization": "Bearer " + sub["token"]}).status_code == 404
        )


def test_one_time_login_csrf_origin_host_and_expiry(setup):
    _, _, sub, state, _, client = setup
    assert client.get("/admin/api/devices").status_code == 401
    assert (
        client.post(
            "/admin/session", json={"code": sub["token"]}, headers={"Origin": ORIGIN}
        ).status_code
        == 401
    )
    code = state.code()
    response = client.post("/admin/session", json={"code": code}, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert (
        "HttpOnly" in response.headers["set-cookie"]
        and "SameSite=strict" in response.headers["set-cookie"]
    )
    assert (
        client.post("/admin/session", json={"code": code}, headers={"Origin": ORIGIN}).status_code
        == 401
    )
    assert client.post("/admin/api/events", json={}, headers={"Origin": ORIGIN}).status_code == 403
    assert client.get("/admin/api/overview", headers={"Host": "evil.test"}).status_code == 403
    assert (
        client.post(
            "/admin/api/events", json={}, headers={"Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    assert (
        client.get("/admin/assets/app.js")
        .headers["content-security-policy"]
        .startswith("default-src 'none'")
    )
    with state.connect() as con:
        con.execute("UPDATE sessions SET last=?", (time.time() - 1900,))
    assert client.get("/admin/api/devices").status_code == 401


def test_admin_device_queries_actions_and_safe_export(setup):
    control, device, sub, state, logs, client = setup
    headers = login(state, client)
    item = event(
        "ali",
        "ingress",
        "connections",
        "test",
        "connection",
        device_id=device["id"],
        identity_source="credential_only",
        connection_id="one",
        core_epoch="test",
        lifecycle="closed",
        source_ip="192.0.2.2",
        target_host="private.example",
        target_port=443,
        upload_bytes=100,
        download_bytes=200,
    )
    logs.ingest([item])
    assert client.get("/admin/api/devices").json()["devices"][0]["name"] == "JXY"
    response = client.post(
        f"/admin/api/devices/{device['id']}/connections", json={}, headers=headers
    )
    assert response.status_code == 200 and response.json()["total"] == 1
    assert (
        client.post(
            "/admin/api/devices/" + "b" * 32 + "/connections", json={}, headers=headers
        ).json()["total"]
        == 0
    )
    out = client.post("/admin/api/exports", json={}, headers=headers)
    assert out.status_code == 200, out.text
    ident = out.json()["id"]
    download = client.get("/admin/api/exports/" + ident + "/download")
    assert download.status_code == 200
    for secret in [device["id"], "private.example", "192.0.2.2", sub["token"]]:
        assert secret not in download.text
    assert (
        client.post("/admin/api/exports", json={"mode": "internal"}, headers=headers).status_code
        == 422
    )
    action = {
        "action": "subscription.revoke",
        "object_id": sub["id"],
        "key": "a" * 32,
        "confirmed": True,
    }
    for _ in range(2):
        assert client.post("/admin/api/actions", json=action, headers=headers).status_code == 200
    assert control.subscriptions()[0]["revoked_at"] is not None
    with control.db.connect() as con:
        assert (
            con.execute("SELECT COUNT(*) FROM audit WHERE action='subscription.revoke'").fetchone()[
                0
            ]
            == 1
        )
    client.request("DELETE", "/admin/session", json={}, headers=headers)
    assert client.get("/admin/api/exports/" + ident + "/download").status_code == 401
    assert sub["token"].encode() not in state.path.read_bytes()


def test_new_subscription_audit_uses_returned_identity_without_persisting_token(setup):
    control, device, _, state, _, client = setup
    headers = login(state, client)
    request = {
        "action": "subscription.issue",
        "payload": {"device_id": device["id"], "format": "mihomo"},
        "key": "c" * 32,
        "confirmed": True,
    }
    first = client.post("/admin/api/actions", json=request, headers=headers).json()
    replay = client.post("/admin/api/actions", json=request, headers=headers).json()
    assert first["audit_result"] == replay["audit_result"] == "issued"
    assert first["result"]["id"] == replay["result"]["id"]
    assert len(control.subscriptions()) == 2
    with state.connect() as con:
        rows = con.execute("SELECT object FROM audit WHERE result='issued'").fetchall()
    assert rows and all(row["object"] == first["result"]["id"] for row in rows)
    assert first["result"]["token"].encode() not in state.path.read_bytes()


def test_audit_storage_failure_prevents_control_change(setup, monkeypatch):
    control, _, sub, state, _, client = setup
    headers = login(state, client)

    def unavailable(*args, **kwargs):
        raise LogError("ADMIN_STATE_UNAVAILABLE", status=503)

    monkeypatch.setattr(state, "intent", unavailable)
    result = client.post(
        "/admin/api/actions",
        json={
            "action": "subscription.revoke",
            "object_id": sub["id"],
            "key": "a" * 32,
            "confirmed": True,
        },
        headers=headers,
    )
    assert result.status_code == 503
    assert control.subscriptions()[0]["revoked_at"] is None


def test_settings_no_network_revision_or_original_data_change(setup):
    control, device, sub, state, logs, client = setup
    headers = login(state, client)
    before = control.fetch(sub["token"], "mihomo")
    revision = control.publication()
    result = client.post(
        "/admin/api/policy",
        json={
            "changes": {"desired_enabled": False},
            "expected_revision": 2,
            "key": "b" * 32,
            "confirmed": True,
        },
        headers=headers,
    )
    assert result.status_code == 200, result.text
    assert not logs.policy()["desired_enabled"]
    assert control.fetch(sub["token"], "mihomo") == before and control.publication() == revision


def test_export_session_binding_internal_confirmation_and_integrity(setup):
    import hashlib

    _, device, _, state, logs, client = setup
    headers = login(state, client)
    logs.ingest(
        [
            event(
                "ali",
                "ingress",
                "system",
                "test",
                "ssh_failed",
                source_ip="192.0.2.1",
                account="demo",
            )
        ]
    )
    response = client.post(
        "/admin/api/exports", json={"mode": "internal", "confirmed": True}, headers=headers
    )
    ident = response.json()["id"]
    data = client.get("/admin/api/exports/" + ident + "/download")
    meta = client.get("/admin/api/exports/" + ident).json()
    assert "192.0.2.1" in data.text
    assert hashlib.sha256(data.content).hexdigest() == meta["digest"]
    assert len(data.content) == meta["size"]
    login(state, client)  # A second valid session cannot download the first session's file.
    assert client.get("/admin/api/exports/" + ident + "/download").status_code == 404
    with state.connect() as con:
        con.execute("UPDATE exports SET expires=?", (time.time() - 1,))
    state.prune()
    assert not (state.root / "exports" / (ident + ".json")).exists()


def test_shortened_platform_audit_retention(setup):
    _, _, _, state, _, client = setup
    headers = login(state, client)
    with state.connect() as con:
        state.record(con, "synthetic", "session.login", None, "success")
        con.execute("UPDATE audit SET at=?", (time.time() - 2 * 86400,))
    result = client.post(
        "/admin/api/policy",
        json={
            "changes": {"audit_days": 1},
            "expected_revision": 2,
            "key": "c" * 32,
            "confirmed": True,
        },
        headers=headers,
    )
    assert result.status_code == 200
    state.prune()
    with state.connect() as con:
        assert not con.execute("SELECT id FROM audit WHERE at<?", (time.time() - 86400,)).fetchall()


def test_secret_validation_error_response_and_code_only_hashed(setup):
    _, _, _, state, _, client = setup
    code = state.code()
    assert code.encode() not in state.path.read_bytes()
    response = client.post(
        "/admin/session", json={"code": code, "SECRET": "never echo"}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 422 and "SECRET" not in response.text
    headers = login(state, client)
    response = client.post(
        "/admin/api/events", json={"filters": {"target": "https://SECRET/path"}}, headers=headers
    )
    assert response.status_code == 422 and "SECRET" not in response.text


def test_audit_pagination_is_chronological_and_filter_bound(setup):
    _, _, _, state, _, _ = setup
    now = time.time()
    with state.connect() as con:
        for i in range(3):
            con.execute(
                "INSERT INTO audit VALUES(?,?,?,?,?,?)",
                (f"{3 - i:032x}", now - i - 1, "test", "session.login", None, "success"),
            )
    first = state.audit(now - 10, now, limit=1)
    second = state.audit(now - 10, now, limit=1, cursor=first["next_cursor"])
    assert first["items"][0]["at"] > second["items"][0]["at"]
    with pytest.raises(LogError):
        state.audit(now - 20, now, limit=1, cursor=first["next_cursor"])


def test_only_explicit_local_proxy_can_supply_log_request_id(setup):
    control, _, sub, _, _, _ = setup
    for trusted, client_host, accepted in [
        (False, "127.0.0.1", False),
        (True, "192.0.2.1", False),
        (True, "127.0.0.1", True),
    ]:
        rows = []
        client = TestClient(
            create_app(control, "a" * 48, log_sink=rows.append, trusted_log_proxy=trusted),
            client=(client_host, 50000),
        )
        response = client.get(sub["path"], headers={"X-Wayvia-Request-ID": "f" * 32})
        assert response.status_code == 200
        assert (rows[-1]["request_id"] == "f" * 32) == accepted
        assert response.headers["x-request-id"] == rows[-1]["request_id"]
