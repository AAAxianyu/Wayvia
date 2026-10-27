#!/usr/bin/env python3
"""Loopback synthetic UI fixture; no real credentials, hosts or network routing."""

import argparse
import json
import time
from pathlib import Path

import uvicorn
from fastapi.testclient import TestClient

from wayvia.admin.app import create_app
from wayvia.admin.control import ControlGateway
from wayvia.admin.state import AdminState
from wayvia.api import create_app as control_app
from wayvia.config import WayviaConfig, example_config
from wayvia.observability.models import event
from wayvia.observability.store import LogStore
from wayvia.services.control import ControlService, SimulatedDataplane


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18790)
    args = parser.parse_args()
    root = args.directory.resolve()
    control = ControlService(
        root / "control",
        WayviaConfig.model_validate(example_config()),
        mode="test",
        dataplane=SimulatedDataplane(),
    )
    devices = control.devices()
    if not devices:
        for name, platform in [("JXY", "android"), ("LTY", "ios")]:
            device = control.add(name, platform)
            control.issue(device["id"], "mihomo")
        devices = control.devices()
    state = AdminState(root / "admin", create=True)
    logs = LogStore(root / "logs", create=True)
    p = logs.policy()
    logs.update_policy({"desired_enabled": True, "min_free_bytes": 0}, p["revision"])
    now = time.time()
    rows = []
    for i in range(16):
        rows.append(
            event(
                "ali",
                "ingress",
                "connections",
                "demo",
                "connection",
                observed_at=now - i * 40 - 1,
                device_id=devices[i % 2]["id"],
                identity_source="credential_only",
                connection_id=f"demo-{i}",
                core_epoch="demo",
                source_ip=f"192.0.2.{i % 2 + 1}",
                target_host=["video.example", "updates.example"][i % 2],
                target_port=443,
                transport="tcp",
                lifecycle="closed",
                created_at=now - i * 40 - 30,
                closed_at=now - i * 40 - 2,
                upload_bytes=10000 + i * 123,
                download_bytes=300000 + i * 21000,
            )
        )
    for i in range(6):
        rows.append(
            event(
                "ali",
                "ingress",
                "nginx",
                "demo",
                "request",
                observed_at=now - i * 60 - 1,
                request_id=f"{i:032x}",
                route="subscription_download",
                source_ip="192.0.2.10",
                method="GET",
                status=429 if i < 2 else 200,
                limit_rate="REJECTED" if i == 0 else "PASSED",
                limit_concurrent="REJECTED" if i == 1 else "PASSED",
                duration_ms=11,
            )
        )
    rows.extend(
        [
            event("jp", "exit", "system", "demo", "heartbeat", observed_at=now - 200),
            event(
                "ali",
                "ingress",
                "system",
                "demo",
                "ssh_failed",
                source_ip="198.51.100.9",
                account="demo",
                auth_method="password",
            ),
            event(
                "ali", "ingress", "system", "demo", "service_started", unit="wayvia-ingress.service"
            ),
        ]
    )
    logs.ingest(rows)
    print(
        json.dumps(
            {
                "synthetic_only": True,
                "url": f"http://127.0.0.1:{args.port}/admin",
                "login_code": state.code(),
                "root": str(root),
            }
        ),
        flush=True,
    )
    gateway = ControlGateway(
        "demo-only-" + "a" * 40, client=TestClient(control_app(control, "demo-only-" + "a" * 40))
    )
    application = create_app(state, logs, gateway, origin=f"http://127.0.0.1:{args.port}")
    uvicorn.run(
        application, host="127.0.0.1", port=args.port, access_log=False, log_level="critical"
    )


if __name__ == "__main__":
    main()
