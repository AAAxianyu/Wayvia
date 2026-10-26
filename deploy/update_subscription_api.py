#!/usr/bin/env python3
"""Narrow schema-3 subscription API update. No network-agent or data-plane commands.

The candidate is built offline with scripts/build_subscription_overlay.py. Read
and verify this script before use; old private deployment caches are not inputs.
Only --apply performs the API restart and subscription presentation write.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CONTROL = Path("/var/lib/wayvia/control")
INSTALLED = Path("/opt/wayvia/.venv/lib/python3.12/site-packages/wayvia")
DROPIN = Path("/etc/systemd/system/wayvia-api.service.d/50-subscription-client.conf")
UNITS = (
    "wayvia-agent.service",
    "wayvia-ingress.service",
    "wayvia-carrier.service",
    "wayvia-vpn-client.service",
    "wayvia-bridge.service",
    "wayvia-usage.service",
    "nginx.service",
    "wayvia-monitor.timer",
    "wayvia-observe-stability-20261020.service",
)
ALLOWED = {"client/__init__.py", "client/names.py", "client/legacy_api.py", "render/__init__.py"}


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic(path, data, mode=0o600):
    temporary = path.with_name(path.name + ".new")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)


def snapshot():
    with sqlite3.connect(f"file:{CONTROL / 'metadata.db'}?mode=ro", uri=True) as connection:
        devices = connection.execute("SELECT * FROM devices ORDER BY id").fetchall()
        subscriptions = connection.execute(
            "SELECT id,device_id,token_hash,format,expires_at,revoked_at,created_at "
            "FROM subscriptions ORDER BY id"
        ).fetchall()
        settings = connection.execute(
            "SELECT * FROM settings WHERE key IN ('desired_revision','published_revision') "
            "ORDER BY key"
        ).fetchall()
        schema = [
            r[0]
            for r in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
    units = {
        unit: command(
            "systemctl",
            "show",
            unit,
            "--property=MainPID,ActiveState,SubState,ActiveEnterTimestampMonotonic",
        )
        for unit in UNITS
    }
    return {
        "units": units,
        "network_configs": {str(p): sha(p) for p in Path("/etc/wayvia").glob("*") if p.is_file()},
        "original_package": {
            str(p.relative_to(INSTALLED)): sha(p) for p in INSTALLED.rglob("*.py")
        },
        "identity_hash": hashlib.sha256(
            json.dumps([devices, subscriptions, settings]).encode()
        ).hexdigest(),
        "schema": schema,
    }


def healthy():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(30):
        try:
            with opener.open("http://127.0.0.1:8787/healthz", timeout=2) as response:
                if response.status == 200 and json.load(response)["status"] == "alive":
                    return
        except (OSError, ValueError):
            pass
        time.sleep(0.3)
    raise RuntimeError("API readiness failed")


def update(args):
    if os.geteuid() != 0:
        raise RuntimeError("Root is required for the API unit update")
    manifest_path = args.release / "manifest.json"
    if sha(manifest_path) != args.manifest_sha256:
        raise RuntimeError("Candidate manifest mismatch")
    manifest = json.loads(manifest_path.read_text())
    before_files, after_files = manifest["baseline_files"], manifest["release_files"]
    if manifest["storage_migration"] != 3 or set(manifest["overlay_files"]) != ALLOWED:
        raise RuntimeError("Unexpected overlay scope")
    if {
        p
        for p in before_files.keys() | after_files.keys()
        if before_files.get(p) != after_files.get(p)
    } != ALLOWED:
        raise RuntimeError("Unexpected candidate difference")
    for relative, digest in before_files.items():
        if sha(INSTALLED / relative) != digest:
            raise RuntimeError("Installed baseline changed")
    actual = {
        str(p.relative_to(args.release / "wayvia")): sha(p)
        for p in (args.release / "wayvia").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    if actual != after_files or any(p.is_symlink() for p in args.release.rglob("*")):
        raise RuntimeError("Candidate content mismatch")
    before = snapshot()
    if before["schema"] != [1, 2, 3]:
        raise RuntimeError("Expected existing schema-3 database")
    if (
        command("systemctl", "show", "wayvia-api.service", "--property=User", "--value").strip()
        != "wayvia"
    ):
        raise RuntimeError("Unexpected API identity")
    with sqlite3.connect(f"file:{CONTROL / 'metadata.db'}?mode=ro", uri=True) as connection:
        account = connection.execute(
            "SELECT s.revoked_at,s.expires_at,d.status,d.desired_action,s.format "
            "FROM subscriptions s JOIN devices d ON d.id=s.device_id WHERE s.id=?",
            (args.subscription_id,),
        ).fetchone()
    if (
        not account
        or account[0] is not None
        or account[1] <= time.time()
        or tuple(account[2:]) != ("active", None, "mihomo")
    ):
        raise RuntimeError("Expected an existing active Mihomo subscription")
    if not args.apply:
        return {"status": "verified_not_applied", "schema": 3, "changed_files": sorted(ALLOWED)}
    release_id = "subscription-client-" + args.manifest_sha256[:16]
    backup = Path("/root") / ("wayvia-" + release_id)
    backup.mkdir(mode=0o700)
    (backup / "before.json").write_text(json.dumps(before, indent=2))
    with (
        sqlite3.connect(CONTROL / "metadata.db") as source,
        sqlite3.connect(backup / "metadata.db") as target,
    ):
        source.backup(target)
    old_dropin = DROPIN.read_bytes() if DROPIN.exists() else None
    if old_dropin is not None:
        (backup / "previous-dropin.conf").write_bytes(old_dropin)
    shutil.copyfile("/var/lib/wayvia/install-manifest.json", backup / "install-manifest.json")
    release = Path("/opt/wayvia/api-releases") / release_id
    release.parent.mkdir(mode=0o755, exist_ok=True)
    shutil.copytree(args.release, release, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for p in [release, *release.rglob("*")]:
        p.chmod(0o755 if p.is_dir() else 0o644)
    sys.path.insert(0, str(release))
    from wayvia.client import https_url
    from wayvia.client.names import set_display_name
    from wayvia.storage import Database

    origin = https_url(args.public_origin)
    if origin.path not in {"", "/"} or origin.query:
        raise RuntimeError("Expected an HTTPS origin")
    # Neither this argument nor the release path may contain systemd expansion syntax.
    if any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:/.[]-_"
        for c in args.public_origin
    ):
        raise RuntimeError("Unexpected public origin characters")
    db = Database(CONTROL / "metadata.db")
    key = "subscription_title:" + args.subscription_id
    with db.connect() as connection:
        previous_name = connection.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
    DROPIN.parent.mkdir(mode=0o755, exist_ok=True)
    unit = (
        "[Service]\nEnvironment=PYTHONPATH="
        + str(release)
        + "\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n"
        "ExecStart=\nExecStart=/opt/wayvia/.venv/bin/python -m wayvia.client.legacy_api"
        " --host 127.0.0.1 --port 8787 --public-origin " + args.public_origin.rstrip("/") + "\n"
    )
    try:
        atomic(DROPIN, unit.encode(), 0o644)
        command("systemctl", "daemon-reload")
        command("systemctl", "restart", "wayvia-api.service")
        healthy()
        set_display_name(db, args.subscription_id, args.name)
        after = snapshot()
        if before != after:
            raise RuntimeError("Protected runtime state changed during the API update")
        installation = json.loads((backup / "install-manifest.json").read_text())
        installation["subscription_api_update"] = {
            "release": str(release),
            "manifest_sha256": args.manifest_sha256,
            "dropin": str(DROPIN),
            "dropin_sha256": sha(DROPIN),
            "backup": str(backup),
            "storage_migration": 3,
            "only_restarted_unit": "wayvia-api.service",
        }
        atomic(
            Path("/var/lib/wayvia/install-manifest.json"),
            json.dumps(installation, indent=2).encode(),
        )
        result = {
            "status": "applied",
            "schema": 3,
            "protected_runtime_unchanged": True,
            "only_restarted_unit": "wayvia-api.service",
            "release": str(release),
            "manifest_sha256": args.manifest_sha256,
            "backup": str(backup),
        }
        (backup / "after.json").write_text(json.dumps(after, indent=2))
        (backup / "result.json").write_text(json.dumps(result, indent=2))
        return result
    except BaseException:
        # Restore only the presentation key and API unit. Never restore an old database:
        # doing so could resurrect revoked credentials or rewind concurrent telemetry.
        with db.change_lock(), db.connect() as connection:
            if previous_name is None:
                connection.execute("DELETE FROM settings WHERE key=?", (key,))
            else:
                connection.execute(
                    "UPDATE settings SET value=? WHERE key=?", (previous_name[0], key)
                )
        if old_dropin is None:
            DROPIN.unlink(missing_ok=True)
        else:
            atomic(DROPIN, old_dropin, 0o644)
        command("systemctl", "daemon-reload")
        command("systemctl", "restart", "wayvia-api.service")
        healthy()
        raise


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--subscription-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(update(parser.parse_args())))
