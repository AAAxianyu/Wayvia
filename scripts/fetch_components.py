#!/usr/bin/env python3
"""Download pinned official release assets and reject checksum mismatches."""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fetch(arch: str, output: Path) -> None:
    lock = json.loads((ROOT / "deploy/lab/components.lock.json").read_text())
    output.mkdir(parents=True, exist_ok=True)
    for name, component in lock["components"].items():
        asset = component["assets"][arch]
        archive = output / asset["name"]
        if not archive.exists():
            temporary = archive.with_suffix(archive.suffix + ".partial")
            with urllib.request.urlopen(asset["url"], timeout=120) as response:
                with temporary.open("wb") as target:
                    shutil.copyfileobj(response, target)
            temporary.replace(archive)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != asset["sha256"]:
            raise RuntimeError(f"{asset['name']}: checksum mismatch")
        binary = output / name
        if name == "sing-box":
            with tarfile.open(archive) as stream:
                member = next(m for m in stream.getmembers() if m.name.endswith("/sing-box"))
                with stream.extractfile(member) as source, binary.open("wb") as target:
                    shutil.copyfileobj(source, target)
        elif name == "mihomo":
            with gzip.open(archive) as source, binary.open("wb") as target:
                shutil.copyfileobj(source, target)
        elif name == "v2ray":
            with zipfile.ZipFile(archive) as stream:
                binary.write_bytes(stream.read("v2ray"))
                for data in ["geoip.dat", "geosite.dat"]:
                    (output / data).write_bytes(stream.read(data))
        else:
            shutil.copyfile(archive, binary)
        os.chmod(binary, 0o755)
        print(f"{name} {component['version']}: SHA256 verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", choices=["arm64", "amd64"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fetch(args.arch, args.output)
