#!/usr/bin/env python3
"""Software-only schema-3 regression. Run with PYTHONPATH set to the built release."""

import json
import resource
import tempfile
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from wayvia.client import NODE
from wayvia.client.legacy_api import create_legacy_app
from wayvia.client.names import set_display_name
from wayvia.config import WayviaConfig, example_config
from wayvia.services.control import ControlService, SimulatedDataplane
from wayvia.storage import MIGRATIONS


def main():
    assert set(MIGRATIONS) == {1, 2, 3}, "This regression must use the real schema-3 baseline"
    # A contract fixture does not need the full production domain catalogue. Keep
    # this process bounded even when an operator runs it outside a development VM.
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    fixture = example_config()
    fixture["rulesets"]["domestic-rules"] = {
        "source": "Synthetic API contract fixture",
        "version": "test-1",
        "cidrs": ["223.5.5.0/24"],
        "domains": [],
    }
    fixture["rulesets"]["domestic-domains"] = {
        "source": "Synthetic API contract fixture",
        "version": "test-1",
        "cidrs": [],
        "domains": ["cn.example"],
    }
    with tempfile.TemporaryDirectory(prefix="wayvia-subscription-software-") as root:
        service = ControlService(
            Path(root),
            WayviaConfig.model_validate(fixture),
            mode="test",
            dataplane=SimulatedDataplane(),
        )
        phone = service.add("phone-primary", "android")
        desktop = service.add("JXY", "desktop")
        phone_link = service.issue(phone["id"], "mihomo")
        desktop_link = service.issue(desktop["id"], "mihomo")
        before = service.export(phone["id"], "mihomo")
        with service.db.connect() as connection:
            devices = [tuple(r) for r in connection.execute("SELECT * FROM devices ORDER BY id")]
            links = [
                tuple(r) for r in connection.execute("SELECT * FROM subscriptions ORDER BY id")
            ]

        def forbidden(*args, **kwargs):
            raise AssertionError("An API presentation update must not touch the dataplane")

        service.dataplane.sync = forbidden
        service.dataplane.block = forbidden
        set_display_name(service.db, phone_link["id"], "LTY")
        with service.db.connect() as connection:
            assert devices == [
                tuple(r) for r in connection.execute("SELECT * FROM devices ORDER BY id")
            ]
            assert links == [
                tuple(r) for r in connection.execute("SELECT * FROM subscriptions ORDER BY id")
            ]
        assert before == service.export(phone["id"], "mihomo")
        client = TestClient(
            create_legacy_app(service, "a" * 48, public_origin="https://sub.example:9443"),
            base_url="https://sub.example",
        )
        info = phone_link["path"].rsplit("/", 1)[0] + "/info"
        assert client.get(info).json()["title"] == "LTY"
        assert service.subscriptions()[0]["fetch_count"] == 0
        response = client.get(phone_link["path"])
        assert response.status_code == 200
        assert 'filename="LTY.yaml"' in response.headers["content-disposition"]
        assert response.headers["profile-web-page-url"] == "https://sub.example:9443" + info
        assert response.headers["profile-update-interval"] == "24"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "subscription-userinfo" not in response.headers
        profile = yaml.safe_load(response.text)
        assert profile["proxies"][0]["name"] == NODE
        assert profile["proxy-groups"][0]["proxies"] == [NODE]
        assert profile["proxy-groups"][0]["interval"] == 0
        assert profile["proxy-groups"][0]["empty-fallback"] == "REJECT"
        assert "JXY" in client.get(desktop_link["path"]).headers["content-disposition"]
        assert client.get("/api/v1/devices").status_code == 401
        assert (
            client.get(
                "/api/v1/devices", headers={"Authorization": "Bearer " + "a" * 48}
            ).status_code
            == 200
        )
        assert client.get(phone_link["path"].replace("mihomo", "sing-box")).status_code == 401
        for value in (phone_link["token"], "secret_ref", "password", "token_hash"):
            assert value not in client.get(info).text
        service.revoke_subscription(phone_link["id"])
        assert client.get(phone_link["path"]).status_code == client.get(info).status_code == 401
        with service.db.connect() as connection:
            assert {r[0] for r in connection.execute("SELECT version FROM schema_migrations")} == {
                1,
                2,
                3,
            }
            assert (
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='usage_history'"
                ).fetchone()
                is None
            )
    print(
        json.dumps(
            {
                "result": "passed",
                "schema": 3,
                "external_requests": 0,
                "dataplane_calls_after_setup": 0,
                "name": "LTY",
            }
        )
    )


if __name__ == "__main__":
    main()
