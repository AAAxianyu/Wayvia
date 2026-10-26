"""Offline two-role incident example. Uses synthetic records, never a sampler."""

import argparse
import json
import tempfile
import time
from pathlib import Path

from wayvia.evidence import context
from wayvia.telemetry.records import SOURCES, Policy, digest, stamp
from wayvia.telemetry.report import export_bundle, timeline, verify_bundle
from wayvia.telemetry.store import DiagnosticStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = time.time() - 600
    revision = "a" * 64
    with tempfile.TemporaryDirectory(prefix="wayvia-evidence-demo-") as temporary:
        root = Path(temporary).resolve()
        stores = [
            DiagnosticStore(
                root / role,
                {
                    "role": role,
                    "revision": revision,
                    "generation": ("b" if role == "ingress" else "c") * 32,
                    "path_id": "simulated-pair",
                    "run_id": "simulated-" + role,
                },
                Policy(buffer_seconds=300),
            )
            for role in ("ingress", "exit")
        ]
        incident = None
        for offset in range(0, 605, 5):
            for store in stores:
                record = {
                    **context(
                        role=store.metadata["role"],
                        revision=revision,
                        generation=store.metadata["generation"],
                        run_id=store.metadata["run_id"],
                        observed_at=stamp(base + offset),
                    ),
                    "type": "sample",
                    "event_id": digest([store.metadata["role"], offset]),
                    "sources": dict.fromkeys(SOURCES, "ok"),
                    "tcp": [{"id": "e" * 16, "rtt_ms": 20, "send_q_bytes": 0}],
                    "collector": {"duration_ms": 1, "cpu_ms": 0.1, "rss_bytes": 4096},
                }
                store.append(record)
            if offset in {240, 260}:
                kind = "probe_failed" if offset == 240 else "recovery_completed"
                record = {
                    **context(
                        role="ingress",
                        revision=revision,
                        generation="b" * 32,
                        observed_at=stamp(base + offset),
                    ),
                    "type": "event",
                    "kind": kind,
                    "layer": "egress",
                    "source": "agent",
                    "status": "failed" if offset == 240 else "healthy",
                    "event_id": digest(["simulated", kind]),
                }
                returned = stores[0].append(record)
                incident = returned or incident
                stores[1].import_trigger(stores[0].triggers()[0], now=base + offset)
        report = timeline(
            [store.path for store in stores],
            incident_id=incident,
            timezone="America/Los_Angeles",
            now=base + 600,
        )
        assert all(report["coverage"][role]["fraction"] == 1 for role in ("ingress", "exit"))
        exported = export_bundle(report, args.output / "synthetic-evidence.zip")
        assert verify_bundle(args.output / "synthetic-evidence.zip")["status"] == "verified"
        result = {
            "mode": "simulated",
            "network_requests": 0,
            "health_or_route_mutations": 0,
            "incident_id": incident,
            "coverage": report["coverage"],
            "export": exported,
        }
        (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))


if __name__ == "__main__":
    main()
