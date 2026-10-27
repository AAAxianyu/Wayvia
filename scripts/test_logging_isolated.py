#!/usr/bin/env python3
"""Non-destructive logging acceptance using only disposable, externally disconnected containers."""

import argparse
import importlib.util
import json
import subprocess
import tempfile
import uuid
from pathlib import Path

from wayvia.observability.nginx import render

ROOT = Path(__file__).resolve().parents[1]
NGINX = "nginx@sha256:f6daac2445b0ce70e64d77442ccf62839f3f1b4c24bf6746a857eff014e798c8"
CLIENT = "wayvia-lab:locked"


def run(argv, **kwargs):
    result = subprocess.run(argv, check=False, text=True, timeout=120, **kwargs)
    if result.returncode:
        print(result.stdout or "", flush=True)
        print(result.stderr or "", flush=True)
        result.check_returncode()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.dependencies.is_dir():
        raise RuntimeError("Locked Linux dependency environment unavailable")
    mounts = [
        "-v",
        str(ROOT / "src") + ":/repo/src:ro",
        "-v",
        str(ROOT / "tests") + ":/repo/tests:ro",
        "-v",
        str(args.dependencies.resolve()) + ":/python:ro",
        "-e",
        "PYTHONPATH=/repo/src:/python",
    ]
    core = run(
        [
            "docker",
            "run",
            "--rm",
            "--label",
            "org.wayvia.disposable=logging",
            "--network",
            "none",
            "--memory",
            "384m",
            "--cpus",
            "1",
            "--pids-limit",
            "128",
            *mounts,
            "--entrypoint",
            "python3",
            CLIENT,
            "/repo/tests/lab/logging_core_probe.py",
        ],
        capture_output=True,
    )
    report = {
        "scope": "disposable_normal_paths_only_no_production",
        "core": json.loads(core.stdout),
    }
    spec = importlib.util.spec_from_file_location("limits", ROOT / "deploy/subscription_limits.py")
    limits = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(limits)
    baseline = limits.render(args.original.read_bytes()).decode()
    for mode in ("baseline", "enabled"):
        name = "wayvia-logging-" + uuid.uuid4().hex[:12]
        with tempfile.TemporaryDirectory(prefix="logging-nginx-", dir=ROOT / ".cache") as directory:
            root = Path(directory)
            root.chmod(0o755)
            site = render(baseline, "/logs/events.sock") if mode == "enabled" else baseline
            site = site.replace("/etc/wayvia/secrets/entry-tls.crt", "/lab/server.crt").replace(
                "/etc/wayvia/secrets/entry-tls.key", "/lab/server.key"
            )
            (root / "nginx.conf").write_text(
                "worker_processes 1;\npid /tmp/nginx.pid;\n"
                "events {worker_connections 128;}\nhttp {\n" + site + "\n}\n"
            )
            run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "-v",
                    str(root) + ":/lab",
                    "--entrypoint",
                    "openssl",
                    NGINX,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    "/lab/server.key",
                    "-out",
                    "/lab/server.crt",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=127.0.0.1",
                    "-addext",
                    "subjectAltName=IP:127.0.0.1",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            run(
                [
                    "docker",
                    "volume",
                    "create",
                    "--label",
                    "org.wayvia.disposable=logging",
                    name + "-ipc",
                ],
                stdout=subprocess.DEVNULL,
            )
            try:
                run(
                    [
                        "docker",
                        "run",
                        "-d",
                        "--network",
                        "none",
                        "--label",
                        "org.wayvia.disposable=logging",
                        "--memory",
                        "128m",
                        "--cpus",
                        "1",
                        "--pids-limit",
                        "64",
                        "--name",
                        name,
                        "-v",
                        str(root) + ":/lab",
                        "-v",
                        name + "-ipc:/logs",
                        "--entrypoint",
                        "nginx",
                        NGINX,
                        "-c",
                        "/lab/nginx.conf",
                        "-g",
                        "daemon off;",
                    ],
                    stdout=subprocess.DEVNULL,
                )
                result = run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "container:" + name,
                        "--label",
                        "org.wayvia.disposable=logging",
                        "--memory",
                        "256m",
                        "--pids-limit",
                        "64",
                        *mounts,
                        "-v",
                        str(root) + ":/lab",
                        "-v",
                        name + "-ipc:/logs",
                        "-v",
                        str(root / "server.crt") + ":/tmp/lab-cert.pem:ro",
                        "--entrypoint",
                        "python3",
                        CLIENT,
                        "/repo/tests/lab/logging_nginx_probe.py",
                        mode,
                    ],
                    capture_output=True,
                )
                report[mode] = json.loads(result.stdout.splitlines()[-1])
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, indent=2) + "\n")
                logs = run(["docker", "logs", name], capture_output=True)
                for marker in ["A" * 64, "B" * 64, "SECRET_EXTRA", "hold=1"]:
                    assert marker not in logs.stdout + logs.stderr
            finally:
                subprocess.run(
                    ["docker", "rm", "-f", name],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                )
                subprocess.run(
                    ["docker", "volume", "rm", name + "-ipc"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                )
    baseline_p95 = max(r["p95_ms"] for r in report["baseline"]["normal_trials"])
    enabled_p95 = max(r["p95_ms"] for r in report["enabled"]["normal_trials"])
    report["subscription_p95_gate"] = {
        "baseline_ms": baseline_p95,
        "enabled_ms": enabled_p95,
        "allowed_increment_ms": max(100, baseline_p95 * 0.1),
        "passed": enabled_p95 - baseline_p95 <= max(100, baseline_p95 * 0.1),
    }
    report["images"] = {"core": CLIENT, "nginx": NGINX}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    assert report["subscription_p95_gate"]["passed"]


if __name__ == "__main__":
    main()
