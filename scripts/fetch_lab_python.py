#!/usr/bin/env python3
"""Prepare hash-locked Linux wheels for the isolated executor experiment."""

import argparse
import hashlib
import json
import shutil
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fetch(arch):
    lock = json.loads((ROOT / "deploy/lab/python-runtime.lock.json").read_text())
    directory = ROOT / ".cache/lab-python" / arch
    directory.mkdir(parents=True, exist_ok=True)
    for asset in lock["architectures"][arch]:
        path = directory / asset["name"]
        if not path.exists():
            with (
                urllib.request.urlopen(asset["url"], timeout=120) as response,
                path.open("wb") as stream,
            ):
                shutil.copyfileobj(response, stream)
        if hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
            raise RuntimeError("Python wheel checksum mismatch: " + asset["name"])
    requirements = (
        "\n".join(
            a["package"] + "==" + a["version"] + " --hash=sha256:" + a["sha256"]
            for a in lock["architectures"][arch]
        )
        + "\n"
    )
    (directory / "requirements.txt").write_text(requirements)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", choices=["arm64", "amd64"], required=True)
    fetch(parser.parse_args().arch)
