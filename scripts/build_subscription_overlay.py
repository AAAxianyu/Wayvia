#!/usr/bin/env python3
"""Build an API-only release from a reviewed installed schema-3 package, offline."""

import argparse
import ast
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = {
    "services/control.py": "25da29c09bcc412509457356cc51d90b7ccc38b43d9468453a605260b6a8a469",
    "storage/__init__.py": "4f2f9b043283a1c1c24abbc8232d745923f7ec5a48a3a2cbaf295d245b40f78e",
    "render/__init__.py": "14c67bf83046518b5b8690632ece7f28d69ad89f2a87db5e410a8bcd1c9e81e8",
    "api/__init__.py": "076efd81e00d4a009ff86da0c6af4df7e132c47ed81ae9eafe642129795355d9",
}
OVERLAY = ("client/__init__.py", "client/names.py", "client/legacy_api.py", "render/__init__.py")


def hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


def server_renderer(source):
    tree = ast.parse(source)
    return ast.dump(
        next(node for node in tree.body if getattr(node, "name", None) == "ServerRenderer")
    )


def build(baseline, output):
    before = hashes(baseline)
    if any(before.get(path) != digest for path, digest in BASELINE.items()):
        raise ValueError("Unreviewed API, storage, service or renderer baseline")
    if server_renderer((baseline / "render/__init__.py").read_text()) != server_renderer(
        (ROOT / "src/wayvia/render/__init__.py").read_text()
    ):
        raise ValueError("ServerRenderer changed; an API-only release is not permitted")
    if output.exists():
        raise ValueError("Use a new release directory")
    if any(p.is_symlink() for p in baseline.rglob("*")):
        raise ValueError("Baseline must contain regular source files")
    shutil.copytree(
        baseline, output / "wayvia", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    for path in OVERLAY:
        target = output / "wayvia" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "src/wayvia" / path, target)
    after = hashes(output / "wayvia")
    if {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)} != set(OVERLAY):
        raise ValueError("Unexpected release difference")
    manifest = {
        "schema_version": 1,
        "purpose": "API_only_subscription_client_contract",
        "storage_migration": 3,
        "overlay_files": list(OVERLAY),
        "baseline_files": before,
        "release_files": after,
        "server_renderer_unchanged": True,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline", type=Path, required=True, help="Installed wayvia package directory"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.baseline, args.output)
    print(json.dumps({"overlay_files": result["overlay_files"], "storage_migration": 3}))
