"""Temporary, explicitly simulated control-plane demonstration."""

import argparse
import json
import secrets
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from wayvia.api import create_app
from wayvia.config import WayviaConfig, example_config
from wayvia.services.control import ControlService, SimulatedDataplane


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="wayvia-demo-") as directory:
        config = WayviaConfig.model_validate(example_config())
        control = ControlService(
            Path(directory), config, mode="demo", dataplane=SimulatedDataplane()
        )
        admin = secrets.token_urlsafe(48)
        app = create_app(control, admin)
        client = TestClient(app)
        headers = {"Authorization": "Bearer " + admin, "Idempotency-Key": "demo-device"}
        result = client.post(
            "/api/v1/devices",
            headers=headers,
            json={"name": "demonstration", "platform": "android"},
        )
        assert result.status_code == 200, result.text
        device = result.json()
        headers["Idempotency-Key"] = "demo-subscription"
        sub = client.post(
            "/api/v1/subscriptions",
            headers=headers,
            json={"device_id": device["id"], "format": "sing-box"},
        )
        assert sub.status_code == 200, sub.text
        subscription = sub.json()
        profile = client.get(subscription["path"])
        assert profile.status_code == 200
        assert profile.headers["cache-control"] == "no-store, private"
        assert json.loads(profile.text)["outbounds"]
        headers["Idempotency-Key"] = "demo-revoke-subscription"
        revoked = client.delete(f"/api/v1/subscriptions/{subscription['id']}", headers=headers)
        assert revoked.status_code == 200
        assert client.get(subscription["path"]).status_code == 401
        assert control.device(device["id"])["status"] == "active"
        print(
            json.dumps(
                {
                    "mode": "simulated",
                    "storage": "temporary SQLite",
                    "device": device["id"],
                    "profile": "sing-box complete",
                    "subscription_revoke": "verified",
                    "device_permission": "active",
                    "dataplane": "simulated, no network changes",
                }
            )
        )
        if args.serve:
            import uvicorn

            print(f"Temporary demo at http://127.0.0.1:8787; administrator token: {admin}")
            print("Stop with Ctrl-C; data and credentials are removed.")
            uvicorn.run(app, host="127.0.0.1", port=8787, access_log=False)


if __name__ == "__main__":
    main()
