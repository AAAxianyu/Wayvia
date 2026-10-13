#!/usr/bin/env python3
"""Generate real profiles/server artifacts, then run pinned parsers in Docker."""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from lab_config import generate

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()
    if not args.no_build:
        subprocess.run([sys.executable, str(ROOT / "scripts/lab.py"), "--preflight"], check=True)
    output = ROOT / "docs/validation/latest-configs"
    output.mkdir(parents=True, exist_ok=True)
    (ROOT / ".cache").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="generated-configs-", dir=ROOT / ".cache") as temporary:
        generated = Path(temporary)
        generate(generated, laboratory=False)
        command = [
            "docker",
            "run",
            "--rm",
            "--label",
            "org.wayvia.disposable=configs",
            "--privileged",
            "--network",
            "none",
            "-v",
            str(ROOT / "deploy/lab") + ":/opt/wayvia-lab:ro",
            "-v",
            str(generated) + ":/generated:ro",
            "-v",
            str(output) + ":/evidence",
            "--entrypoint",
            "python3",
            "wayvia-lab:locked",
            "/opt/wayvia-lab/validate_configs.py",
        ]
        return subprocess.run(command).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        print("CONFIG ENVIRONMENT UNAVAILABLE: " + str(error), file=sys.stderr)
        sys.exit(4)
