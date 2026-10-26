"""Unified subscription contract through real SQLite and ASGI; no external requests."""

import json
import time
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from wayvia.api import create_app
from wayvia.cli import app
from wayvia.client import GROUP, NODE, TEST_URL, import_link
from wayvia.config import WayviaConfig, example_config
from wayvia.services.control import ControlService, SimulatedDataplane
from wayvia.services.usage import UsageStore


@pytest.fixture
def control(tmp_path):
    return ControlService(
        tmp_path,
        WayviaConfig.model_validate(example_config()),
        mode="test",
        dataplane=SimulatedDataplane(),
    )


def test_import_name_homepage_and_download_metadata_are_safe(control):
    device = control.add("JXY / 电脑\r\nInjected: secret", "desktop")
    issued = control.issue(device["id"], "mihomo")
    client = TestClient(
        create_app(control, "a" * 48, subscriptions_only=True), base_url="https://sub.example"
    )
    response = client.get(issued["path"], headers={"User-Agent": "clash-verge/v2.5.8"})
    assert response.status_code == 200
    header = response.headers["content-disposition"]
    assert header.isascii() and "\r" not in header and "\n" not in header
    assert "电脑" in unquote(header) and "/" not in unquote(header).split("filename*=")[-1]
    assert response.headers["profile-update-interval"] == "24"
    assert (
        response.headers["profile-web-page-url"]
        == "https://sub.example" + issued["path"].rsplit("/", 1)[0] + "/info"
    )
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "subscription-userinfo" not in response.headers  # No fictional quota or service expiry.
    payload = yaml.safe_load(response.text)
    assert payload["proxies"][0]["name"] == NODE
    assert payload["proxy-groups"][0]["name"] == GROUP
    assert payload["proxy-groups"][0]["url"] == TEST_URL
    assert payload["proxy-groups"][0]["interval"] == 0
    raw_token_link = "https://sub.example" + issued["path"] + "?a=1&b=2"
    encoded = import_link(raw_token_link)
    assert parse_qs(urlsplit(encoded).query)["url"] == [raw_token_link]
    assert issued["token"] not in json.dumps(control.subscriptions())
    with control.db.connect() as conn:
        row = conn.execute("SELECT client,version FROM usage_fetch_history").fetchone()
        assert tuple(row) == ("clash-verge", "2.5.8")


def test_names_and_choice_persist_across_rotation_and_no_os_branch(control):
    device = control.add("desktop", "desktop")
    issued = control.issue(device["id"], "mihomo")
    a = control.fetch_document(issued["token"], "mihomo", client_claim="clash-verge/2.5.8")
    control.rotate(device["id"])
    b = control.fetch_document(issued["token"], "mihomo", client_claim="unrecognized-platform")
    first, second = yaml.safe_load(a["text"]), yaml.safe_load(b["text"])
    assert first["proxies"][0]["password"] != second["proxies"][0]["password"]
    assert first["proxies"][0]["name"] == second["proxies"][0]["name"] == NODE
    assert first["proxy-groups"] == second["proxy-groups"]
    assert a["headers"] == b["headers"]
    assert second["profile"]["store-selected"] is True
    assert second["proxy-groups"][0]["empty-fallback"] == "REJECT"
    assert "DIRECT" not in second["proxy-groups"][0]["proxies"]


def test_info_separates_usage_subscription_expiry_and_unknown_entitlement(control):
    device = control.add("my computer", "desktop")
    issued = control.issue(device["id"], "mihomo")
    observed = time.time()
    UsageStore(control.db).record(
        {
            "observed_at": observed,
            "generation": "synthetic",
            "closed_buffer_full": False,
            "oldest_closed_at": None,
            "connections": [
                {
                    "id": "c1",
                    "device_id": device["id"],
                    "created_at": observed - 5,
                    "closed_at": None,
                    "upload": 123,
                    "download": 456,
                }
            ],
        }
    )
    client = TestClient(create_app(control, "a" * 48, subscriptions_only=True))
    response = client.get(issued["path"].rsplit("/", 1)[0] + "/info")
    assert response.status_code == 200
    info = response.json()
    assert info["usage"]["upload_bytes"] == 123 and info["usage"]["download_bytes"] == 456
    assert info["usage"]["subscription_fetches"] == 0  # Opening info isn't a config download.
    assert info["download_link_expires_at"] == issued["expires_at"]
    assert info["client"]["service_expires_at"] is None and info["client"]["quota_bytes"] is None
    assert info["client"]["platform_settings_required"] is False
    for value in (
        issued["token"],
        "secret_ref",
        "token_hash",
        "password",
        control.config.carrier.endpoint,
    ):
        assert value not in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
    assert client.get("/api/v1/subscriptions").status_code == 404
    UsageStore(control.db).failure("USAGE_UNAVAILABLE")
    assert client.get(issued["path"].rsplit("/", 1)[0] + "/info").json()["usage"]["online"] is None
    control.revoke_subscription(issued["id"])
    assert client.get(issued["path"]).status_code == 401
    assert client.get(issued["path"].rsplit("/", 1)[0] + "/info").status_code == 401


def test_expired_or_revoked_accounts_cannot_fetch_metadata(control):
    device = control.add("revoked", "desktop")
    issued = control.issue(device["id"], "mihomo")
    client = TestClient(create_app(control, "a" * 48))
    with control.db.connect() as conn:
        conn.execute("UPDATE subscriptions SET expires_at=0 WHERE id=?", (issued["id"],))
    assert client.get(issued["path"]).status_code == 401
    assert client.get(issued["path"].rsplit("/", 1)[0] + "/info").status_code == 401
    next_link = control.issue(device["id"], "mihomo")
    control.revoke_device(device["id"])
    assert client.get(next_link["path"]).status_code == 401
    assert client.get(next_link["path"].rsplit("/", 1)[0] + "/info").status_code == 401


def test_bad_import_url_is_rejected_and_cli_has_no_network(tmp_path):
    from wayvia.client import subscription_url
    from wayvia.errors import WayviaError

    for value in (
        "http://sub.example/x",
        "https://user:pass@sub.example/x",
        "https://sub.example/x#secret",
        "https://sub.example/not-a-subscription",
    ):
        with pytest.raises(WayviaError):
            import_link(value)
    with pytest.raises(WayviaError):
        subscription_url("https://sub.example/nested", "/subscriptions/" + "x" * 64 + "/mihomo")
    runner = CliRunner()
    url = "https://sub.example/subscriptions/" + "x" * 64 + "/mihomo"
    result = runner.invoke(app, ["subscription", "import-link", url])
    assert result.exit_code == 0
    assert parse_qs(urlsplit(json.loads(result.stdout)["import_url"]).query)["url"] == [url]
    result = runner.invoke(app, ["profile", "capabilities"])
    assert result.exit_code == 0 and not json.loads(result.stdout)["platform_settings_required"]
    result = runner.invoke(app, ["profile", "measure"])
    assert result.exit_code != 0 and "CLIENT_NETWORK_NOT_AUTHORIZED" in result.stdout


def test_exact_subscription_name_preserves_devices_credentials_and_other_links(
    control, monkeypatch
):
    from wayvia.client.names import set_display_name

    phone = control.add("phone-primary", "android")
    desktop = control.add("JXY", "desktop")
    phone_link = control.issue(phone["id"], "mihomo")
    desktop_link = control.issue(desktop["id"], "mihomo")
    before = control.export(phone["id"], "mihomo")
    with control.db.connect() as conn:
        devices = [tuple(r) for r in conn.execute("SELECT * FROM devices ORDER BY id")]
        links = [tuple(r) for r in conn.execute("SELECT * FROM subscriptions ORDER BY id")]

    def forbidden(*args, **kwargs):
        pytest.fail("Naming must not call the dataplane")

    monkeypatch.setattr(control.dataplane, "sync", forbidden)
    monkeypatch.setattr(control.dataplane, "block", forbidden)
    result = set_display_name(control.db, phone_link["id"], "LTY")
    assert result["display_name"] == "LTY"
    with control.db.connect() as conn:
        assert devices == [tuple(r) for r in conn.execute("SELECT * FROM devices ORDER BY id")]
        assert links == [tuple(r) for r in conn.execute("SELECT * FROM subscriptions ORDER BY id")]
    assert before == control.export(phone["id"], "mihomo")
    client = TestClient(create_app(control, "a" * 48), base_url="https://sub.example")
    response = client.get(phone_link["path"])
    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        "attachment; filename=\"LTY.yaml\"; filename*=UTF-8''LTY.yaml"
    )
    assert client.get(phone_link["path"].rsplit("/", 1)[0] + "/info").json()["title"] == "LTY"
    assert "JXY" in unquote(client.get(desktop_link["path"]).headers["content-disposition"])


def test_subscription_name_validation_and_cli(control):
    from wayvia.client.names import display_name, set_display_name
    from wayvia.errors import WayviaError

    issued = control.issue(control.add("phone", "android")["id"], "mihomo")
    for name in ("", "\r\n", '/\\"<>|?*:'):
        with pytest.raises(WayviaError, match="visible"):
            set_display_name(control.db, issued["id"], name)
    with pytest.raises(WayviaError, match="does not exist"):
        set_display_name(control.db, "missing", "LTY")
    (control.root / "config.yaml").write_text(
        yaml.safe_dump(control.config.model_dump(mode="json"))
    )
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(control.root),
            "--config",
            str(control.root / "config.yaml"),
            "--mode",
            "demo",
            "subscription",
            "name",
            issued["id"],
            "LTY",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert display_name(control.db, issued["id"]) == "LTY"


def test_legacy_adapter_routes_do_not_bypass_auth_or_expose_administration(control):
    from wayvia.client.legacy_api import create_legacy_app
    from wayvia.client.names import set_display_name

    issued = control.issue(control.add("phone", "android")["id"], "mihomo")
    set_display_name(control.db, issued["id"], "LTY")
    client = TestClient(
        create_legacy_app(
            control, "a" * 48, subscriptions_only=True, public_origin="https://sub.example:9443"
        ),
        base_url="https://sub.example",
    )
    info = issued["path"].rsplit("/", 1)[0] + "/info"
    assert client.get(info).json()["title"] == "LTY"
    assert control.subscriptions()[0]["fetch_count"] == 0
    response = client.get(issued["path"])
    assert response.status_code == 200
    assert response.headers["profile-web-page-url"] == "https://sub.example:9443" + info
    assert response.headers["referrer-policy"] == "no-referrer"
    assert control.subscriptions()[0]["fetch_count"] == 1
    assert client.get("/api/v1/devices").status_code == 404
    assert client.get(issued["path"].replace("mihomo", "sing-box")).status_code == 401
    control.revoke_subscription(issued["id"])
    assert client.get(info).status_code == 401
    assert client.get(issued["path"]).status_code == 401
