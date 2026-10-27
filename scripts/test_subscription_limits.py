#!/usr/bin/env python3
"""Run real nginx limit checks only inside an isolated disposable Docker namespace."""

import argparse
import importlib.util
import json
import subprocess
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NGINX_IMAGE = "nginx@sha256:f6daac2445b0ce70e64d77442ccf62839f3f1b4c24bf6746a857eff014e798c8"
CLIENT_IMAGE = "wayvia-lab:locked"


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, text=True, timeout=120, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", required=True, type=Path)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("limits", ROOT / "deploy/subscription_limits.py")
    limits = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(limits)
    original = args.original.read_bytes()
    candidate = limits.render(original)
    # Only the certificate paths change; no real secrets are mounted.
    test_site = candidate.replace(b"/etc/wayvia/secrets/entry-tls.crt", b"/lab/server.crt").replace(
        b"/etc/wayvia/secrets/entry-tls.key", b"/lab/server.key"
    )
    if test_site == candidate or b"/etc/wayvia/secrets/" in test_site:
        raise ValueError("Unexpected certificate paths in nginx baseline")
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    name = "wayvia-subscription-test-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="subscription-limits-", dir=cache) as directory:
        testdir = Path(directory)
        testdir.chmod(0o700)
        (testdir / "nginx.conf").write_bytes(
            b"worker_processes 1;\npid /tmp/nginx.pid;\n"
            b"events { worker_connections 128; }\nhttp {\n" + test_site + b"\n}\n"
        )
        mount = str(testdir) + ":/lab"
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "-v",
                mount,
                "--entrypoint",
                "openssl",
                NGINX_IMAGE,
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
        try:
            run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--network",
                    "none",
                    "--memory",
                    "256m",
                    "--cpus",
                    "1",
                    "--pids-limit",
                    "64",
                    "--name",
                    name,
                    "-v",
                    mount + ":ro",
                    "--entrypoint",
                    "nginx",
                    NGINX_IMAGE,
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
                    "--memory",
                    "384m",
                    "--pids-limit",
                    "64",
                    "-v",
                    str(ROOT / "tests/lab/subscription_limit_probe.py") + ":/tmp/probe.py:ro",
                    "-v",
                    str(testdir / "server.crt") + ":/tmp/lab-cert.pem:ro",
                    "--entrypoint",
                    "python3",
                    CLIENT_IMAGE,
                    "/tmp/probe.py",
                ],
                capture_output=True,
            )
            report = json.loads(result.stdout)
            report.update(
                original_sha256=limits.sha(original),
                candidate_sha256=limits.sha(candidate),
                nginx_image=NGINX_IMAGE,
                nginx_version="1.24.0",
                client_image=CLIENT_IMAGE,
            )
            print(json.dumps(report, sort_keys=True))
        finally:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            )


if __name__ == "__main__":
    main()
