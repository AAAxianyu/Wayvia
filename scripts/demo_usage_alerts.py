#!/usr/bin/env python3
"""Offline synthetic usage/history and alert lifecycle; no sampler or delivery calls."""

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from wayvia.config import WayviaConfig, example_config
from wayvia.evidence import context
from wayvia.services.control import ControlService, SimulatedDataplane
from wayvia.services.usage import UsageStore
from wayvia.services.usage_history import history
from wayvia.telemetry.alerts import AlertConfig, AlertStore
from wayvia.telemetry.records import SOURCES, canonical, stamp
from wayvia.telemetry.report import export_json
from wayvia.telemetry.store import DiagnosticStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    destination = args.output.absolute()
    destination.mkdir(mode=0o700)  # Refuse to overwrite an old example.
    now = 1792972800
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        control = ControlService(
            root / "control",
            WayviaConfig.model_validate(example_config()),
            mode="test",
            dataplane=SimulatedDataplane(),
        )
        device = control.add("synthetic-device", "desktop")["id"]
        usage = UsageStore(control.db)
        for at, up in (
            (now - 60, 1000),
            (now - 55, 1050),
            (now - 50, 0),
            (now - 45, 20),
            (now + 60, 80),
        ):
            usage.record(
                {
                    "observed_at": at,
                    "generation": "synthetic",
                    "closed_buffer_full": False,
                    "oldest_closed_at": None,
                    "connections": [
                        {
                            "id": "synthetic",
                            "device_id": device,
                            "created_at": now - 120,
                            "closed_at": None,
                            "upload": up,
                            "download": 2 * up,
                        }
                    ],
                }
            )
        usage_report = history(control.db, device, start=now - 60, end=now + 120, now=now + 120)
        usage_report["mode"] = "simulated"
        assert usage_report["buckets"][0]["upload_bytes"] == 70
        assert usage_report["buckets"][1]["status"] == "unknown"
        export_json(usage_report, destination / "usage.json")
        stores, source_config = [], []
        for role in ("ingress", "exit"):
            run = root / role
            run.mkdir(mode=0o700)
            meta = {
                "role": role,
                "revision": "a" * 64,
                "generation": ("b" if role == "ingress" else "c") * 32,
                "path_id": "synthetic-pair",
                "run_id": role,
            }
            store = DiagnosticStore(run / "rolling", meta)
            for name, value in {
                "run.json": {
                    "schema_version": 1,
                    "run_id": role,
                    "created_at": stamp(now - 300),
                    "contract": {**meta, "mode": "rolling_passive", "until": stamp(now + 600)},
                },
                "state.json": {"status": "running", "at": stamp(now - 300)},
            }.items():
                path = run / name
                path.write_bytes(canonical(value))
                path.chmod(0o600)
            for at in range(now - 300, now + 1, 5):
                store.append(
                    {
                        **context(
                            role=role,
                            revision=meta["revision"],
                            generation=meta["generation"],
                            observed_at=stamp(at),
                        ),
                        "type": "sample",
                        "sources": dict.fromkeys(SOURCES, "ok"),
                    },
                    now=now,
                )
            store.append(
                {
                    **context(
                        role=role,
                        revision=meta["revision"],
                        generation=meta["generation"],
                        observed_at=stamp(now - 5),
                    ),
                    "type": "event",
                    "kind": "health_healthy",
                },
                now=now,
            )
            stores.append(store)
            source_config.append({"role": role, "database": store.path, "run_directory": run})
        config = AlertConfig.model_validate(
            {
                "watches": [
                    {"id": "synthetic", "path_id": "synthetic-pair", "sources": source_config}
                ]
            }
        )
        state = AlertStore(root / "state" / "alerts.db")

        def disk(_):
            return SimpleNamespace(free=1024**3)

        quiet = state.evaluate(config, now=now, disk_usage=disk)
        assert quiet["outbox_pending"] == 0
        store = stores[0]

        def event(at, kind):
            return {
                **context(
                    role="ingress", revision="a" * 64, generation="b" * 32, observed_at=stamp(at)
                ),
                "type": "event",
                "kind": kind,
            }

        store.append(event(now + 1, "health_degraded"), now=now + 1)
        before = [hashlib.sha256(s.path.read_bytes()).hexdigest() for s in stores]
        opened = state.evaluate(config, now=now + 1, disk_usage=disk)
        assert opened["outbox_pending"] == 1
        assert state.evaluate(config, now=now + 2, disk_usage=disk)["outbox_pending"] == 1
        assert before == [hashlib.sha256(s.path.read_bytes()).hexdigest() for s in stores]
        store.append(event(now + 3, "recovery_completed"), now=now + 3)
        recovered = state.evaluate(config, now=now + 3, disk_usage=disk)
        opened["mode"] = recovered["mode"] = "simulated"
        assert recovered["outbox_pending"] == 2
        export_json(opened, destination / "opened-alert.json")
        export_json(recovered, destination / "recovered-alert.json")
        result = {
            "status": "passed",
            "mode": "simulated",
            "source_unchanged_by_evaluation": True,
            "normal_pending": 0,
            "degraded_pending": 1,
            "recovered_pending": 2,
            "notifications_sent": 0,
            "sampling_started": False,
            "usage_sample_end_upload_bytes": 70,
            "missing_minute": "unknown",
        }
        export_json(result, destination / "result.json")
        print(json.dumps(result))


if __name__ == "__main__":
    main()
