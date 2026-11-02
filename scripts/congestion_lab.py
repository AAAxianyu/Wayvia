#!/usr/bin/env python3
"""Run the ADR 014 congestion experiment in one disposable, network-less container."""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from lab_config import generate

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "wayvia-lab:locked"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="docs/validation/congestion-lab-2026-11-02")
    parser.add_argument("--seconds", default="60")
    parser.add_argument("--streams", default="4")
    parser.add_argument("--provider-limit", default="256kb")
    parser.add_argument("--only", nargs="*", default=[])
    args = parser.parse_args()
    output = ROOT / args.output
    output.mkdir(parents=True, exist_ok=True)
    (ROOT / ".cache").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="generated-lab-", dir=ROOT / ".cache") as temporary:
        generate(Path(temporary), laboratory=True)
        command = [
            "docker",
            "run",
            "--rm",
            "--label",
            "org.wayvia.disposable=congestion",
            "--privileged",
            "--network",
            "none",
            "-v",
            str(ROOT / "deploy/lab") + ":/opt/wayvia-lab:ro",
            "-v",
            str(output) + ":/evidence",
            "-v",
            str(ROOT / "src") + ":/workspace/src:ro",
            "-v",
            temporary + ":/generated:ro",
            "-e",
            "PYTHONPATH=/workspace/src",
            "--entrypoint",
            "python3",
            IMAGE,
            "/opt/wayvia-lab/congestion.py",
            "--seconds",
            args.seconds,
            "--streams",
            args.streams,
            "--provider-limit",
            args.provider_limit,
        ] + (["--only", *args.only] if args.only else [])
        try:
            return subprocess.run(command).returncode
        except FileNotFoundError:
            print("LAB ENVIRONMENT UNAVAILABLE: docker", file=sys.stderr)
            return 4


if __name__ == "__main__":
    sys.exit(main())
