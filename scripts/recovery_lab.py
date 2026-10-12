#!/usr/bin/env python3
"""Run real systemd recovery inside a disposable container-private cgroup namespace."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "wayvia-recovery-disposable"
IMAGE = "wayvia-lab-systemd:locked"


def clean():
    result = subprocess.run(["docker", "inspect", NAME], capture_output=True, text=True)
    if result.returncode:
        return 0
    labels = json.loads(result.stdout)[0]["Config"].get("Labels", {}) or {}
    if labels.get("org.wayvia.disposable") != "recovery":
        raise RuntimeError("refusing to remove an unowned recovery container")
    return subprocess.run(["docker", "stop", "--timeout", "10", NAME]).returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()
    if args.clean:
        return clean()
    output = ROOT / "docs/validation/latest-lab"
    output.mkdir(parents=True, exist_ok=True)
    evidence = {
        "status": "initializing",
        "container": NAME,
        "cgroup_mode": "private",
        "host_cgroup_bind": False,
    }
    started = False
    try:
        if not args.no_build:
            subprocess.run(
                [
                    "docker",
                    "build",
                    "-t",
                    IMAGE,
                    "-f",
                    str(ROOT / "deploy/lab/Dockerfile.systemd"),
                    str(ROOT),
                ],
                check=True,
            )
        command = [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            NAME,
            "--label",
            "org.wayvia.disposable=recovery",
            "--privileged",
            "--network",
            "none",
            "--cgroupns",
            "private",
            "--tmpfs",
            "/run",
            "--tmpfs",
            "/run/lock",
            "--tmpfs",
            "/tmp",
            "-v",
            str(ROOT / "src") + ":/workspace/src:ro",
            "-v",
            str(ROOT / "tests/lab") + ":/workspace/tests/lab:ro",
            "-v",
            str(output) + ":/evidence",
            IMAGE,
        ]
        subprocess.run(command, check=True, capture_output=True, text=True)
        started = True
        for _attempt in range(100):
            ready = subprocess.run(
                ["docker", "exec", NAME, "systemctl", "is-system-running"],
                capture_output=True,
                text=True,
            )
            if ready.stdout.strip() in ("running", "degraded"):
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("container-private systemd did not become ready")
        evidence["pid1"] = subprocess.check_output(
            ["docker", "exec", NAME, "cat", "/proc/1/comm"], text=True
        ).strip()
        evidence["cgroup"] = subprocess.check_output(
            ["docker", "exec", NAME, "cat", "/proc/1/cgroup"], text=True
        ).strip()
        evidence["mount"] = subprocess.check_output(
            ["docker", "exec", NAME, "findmnt", "/sys/fs/cgroup"], text=True
        ).strip()
        evidence["version"] = subprocess.check_output(
            ["docker", "exec", NAME, "systemctl", "--version"], text=True
        ).splitlines()[0]
        assert evidence["pid1"] == "systemd"
        assert "cgroup2" in evidence["mount"]
        if "docker/" in evidence["cgroup"] or "system.slice/docker" in evidence["cgroup"]:
            raise RuntimeError("recovery container did not receive a private cgroup view")
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-e",
                "WAYVIA_RECOVERY_LAB=1",
                NAME,
                "/opt/wayvia-lab-python/bin/python",
                "/workspace/tests/lab/recovery_probe.py",
            ]
        ).returncode
        evidence["status"] = "passed" if result == 0 else "failed"
        return result
    except (OSError, RuntimeError, subprocess.CalledProcessError, AssertionError) as error:
        evidence.update(status="environment_unavailable", error=str(error))
        if started:
            logs = subprocess.run(["docker", "logs", NAME], capture_output=True, text=True)
            evidence["boot_logs"] = (logs.stdout + logs.stderr)[-8000:]
        print("RECOVERY LAB FAILED: " + str(error), file=sys.stderr)
        return 4
    finally:
        (output / "recovery-environment.json").write_text(json.dumps(evidence, indent=2) + "\n")
        if started:
            clean()


if __name__ == "__main__":
    sys.exit(main())
