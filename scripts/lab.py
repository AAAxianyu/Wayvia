#!/usr/bin/env python3
"""Build and run only the named disposable Wayvia lab container."""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from lab_config import generate

ROOT = Path(__file__).resolve().parents[1]
NAME = "wayvia-m4-disposable"
IMAGE = "wayvia-lab:locked"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()
    if args.clean:
        recovery = subprocess.run(
            [sys.executable, str(ROOT / "scripts/recovery_lab.py"), "--clean"]
        ).returncode
        if recovery:
            return recovery
        result = subprocess.run(["docker", "inspect", NAME], capture_output=True, text=True)
        if result.returncode:
            return 0
        labels = json.loads(result.stdout)[0]["Config"].get("Labels", {}) or {}
        if labels.get("org.wayvia.disposable") != "m4":
            print("refusing to remove an unowned container", file=sys.stderr)
            return 3
        return subprocess.run(["docker", "rm", "-f", NAME]).returncode
    try:
        arch = subprocess.check_output(
            ["docker", "info", "--format", "{{.Architecture}}"], text=True
        ).strip()
        arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(arch, arch)
        if arch not in ("arm64", "amd64"):
            print("unsupported Docker architecture", file=sys.stderr)
            return 4
        if not args.no_build:
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/fetch_components.py"),
                    "--arch",
                    arch,
                    "--output",
                    str(ROOT / ".cache/components" / arch),
                ],
                check=True,
            )
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/fetch_lab_python.py"), "--arch", arch],
                check=True,
            )
            subprocess.run(
                [
                    "docker",
                    "build",
                    "-t",
                    IMAGE,
                    "-f",
                    str(ROOT / "deploy/lab/Dockerfile"),
                    str(ROOT),
                ],
                check=True,
            )
        (ROOT / ".cache").mkdir(exist_ok=True)
        output = ROOT / "docs/validation/latest-lab"
        output.mkdir(parents=True, exist_ok=True)
        cmd = [
            "docker",
            "run",
            "--rm",
            "--name",
            NAME,
            "--label",
            "org.wayvia.disposable=m4",
            "--privileged",
            "--network",
            "none",
            "-v",
            str(ROOT / "deploy/lab") + ":/opt/wayvia-lab:ro",
            "-v",
            str(output) + ":/evidence",
            "-v",
            str(ROOT / "src") + ":/workspace/src:ro",
            "-e",
            "PYTHONPATH=/workspace/src",
            IMAGE,
        ]
        if args.preflight:
            cmd.append("--preflight")
        with tempfile.TemporaryDirectory(prefix="generated-lab-", dir=ROOT / ".cache") as temporary:
            generated = Path(temporary)
            generate(generated, laboratory=True)
            cmd[cmd.index(IMAGE) : cmd.index(IMAGE)] = ["-v", str(generated) + ":/generated:ro"]
            result = subprocess.run(cmd, check=False).returncode
            if result or args.preflight:
                return result
            executor_cmd = [
                "docker",
                "run",
                "--rm",
                "--label",
                "org.wayvia.disposable=executor",
                "--privileged",
                "--network",
                "none",
                "-v",
                str(ROOT / "src") + ":/workspace/src:ro",
                "-v",
                str(ROOT / "tests/lab") + ":/workspace/tests/lab:ro",
                "-v",
                str(output) + ":/evidence",
                "-e",
                "PYTHONPATH=/workspace/src",
                "--entrypoint",
                "/opt/wayvia-lab-python/bin/python",
                IMAGE,
                "/workspace/tests/lab/executor_probe.py",
            ]
            result = subprocess.run(executor_cmd).returncode
            if result:
                return result
            return subprocess.run(
                [sys.executable, str(ROOT / "scripts/recovery_lab.py")]
            ).returncode
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        print("LAB ENVIRONMENT UNAVAILABLE: " + str(error), file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
