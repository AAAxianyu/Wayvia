#!/usr/bin/env python3
"""Offline unified-client contract example. All HTTP is ASGI/MockTransport."""

import argparse
import copy
import hashlib
import json
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import yaml
from fastapi.testclient import TestClient

from wayvia.api import create_app
from wayvia.client import NODE, import_link
from wayvia.client.diagnostics import diagnose
from wayvia.client.measure import measure
from wayvia.client.profiles import inspect, stage
from wayvia.config import WayviaConfig, example_config
from wayvia.errors import WayviaError
from wayvia.services.control import ControlService, SimulatedDataplane
from wayvia.services.usage import UsageStore
from wayvia.telemetry.report import export_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    destination = args.output.absolute()
    destination.mkdir(mode=0o700)
    data = example_config()
    data["policy"]["domestic_domain_rules_ref"] = None
    domestic = data["rulesets"]["domestic-rules"]
    domestic.update(domains=["domestic.example"], cidrs=["1.0.1.0/24"])
    domestic.pop("sha256", None)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp).resolve()
        control = ControlService(
            root / "control",
            WayviaConfig.model_validate(data),
            mode="test",
            dataplane=SimulatedDataplane(),
        )
        device = control.add("示例电脑", "desktop")
        issued = control.issue(device["id"], "mihomo")
        client = TestClient(
            create_app(control, "a" * 48, subscriptions_only=True),
            base_url="https://subscription.example",
        )
        response = client.get(issued["path"], headers={"User-Agent": "clash-verge/v2.5.8"})
        assert response.status_code == 200
        original = yaml.safe_load(response.text)
        checked = inspect(original, original)
        assert checked["status"] == "compatible"
        import_url = import_link("https://subscription.example" + issued["path"])
        assert parse_qs(urlsplit(import_url).query)["url"] == [
            "https://subscription.example" + issued["path"]
        ]
        target = root / "stage" / "profile.yaml"
        stage(original, target, original, selected=NODE)
        before = hashlib.sha256(target.read_bytes()).hexdigest()
        bad = copy.deepcopy(original)
        bad["mode"] = "direct"
        try:
            stage(bad, target, original)
        except WayviaError:
            pass
        else:
            raise AssertionError("unsafe update accepted")
        assert hashlib.sha256(target.read_bytes()).hexdigest() == before
        control.rotate(device["id"])
        updated = yaml.safe_load(client.get(issued["path"]).text)
        assert updated["proxies"][0]["name"] == original["proxies"][0]["name"]
        assert updated["proxies"][0]["password"] != original["proxies"][0]["password"]
        now = time.time()
        UsageStore(control.db).record(
            {
                "observed_at": now,
                "generation": "synthetic",
                "closed_buffer_full": False,
                "oldest_closed_at": None,
                "connections": [
                    {
                        "id": "fixture",
                        "device_id": device["id"],
                        "created_at": now - 5,
                        "closed_at": None,
                        "upload": 1234,
                        "download": 5678,
                    }
                ],
            }
        )
        info = client.get(issued["path"].rsplit("/", 1)[0] + "/info").json()
        assert info["usage"]["total_bytes"] == 6912 and info["client"]["quota_bytes"] is None
        latency = measure(
            authorized=True,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(204, stream=httpx.ByteStream(b""))
            ),
        )
        assert latency["status"] == "measured"
        diagnostic = diagnose(checked)
        report = {
            "mode": "simulated",
            "import_name_header_present": "filename*=" in response.headers["content-disposition"],
            "homepage_matches_original_https_origin": response.headers[
                "profile-web-page-url"
            ].startswith("https://subscription.example/subscriptions/"),
            "one_click_round_trip": True,
            "node": NODE,
            "rule_count": len(original["rules"]),
            "rotation_retains_node_identity": True,
            "invalid_update_preserves_previous_file": True,
            "flow_bytes": info["usage"]["total_bytes"],
            "quota_bytes": None,
            "service_expires_at": None,
            "download_expiry_separate": bool(info["download_link_expires_at"]),
            "real_network_measurements": 0,
            "real_notifications": 0,
            "actual_GUI_acceptance": "not_run",
            "core_configuration": "validated_separately",
        }
        export_json(report, destination / "result.json")
        export_json({**diagnostic, "mode": "simulated"}, destination / "diagnostics.json")
        export_json(
            {**latency, "mode": "simulated_mock_transport"}, destination / "measurement.json"
        )
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
