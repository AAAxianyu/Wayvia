#!/usr/bin/env python3
"""Explicit, data-preserving rolling upgrade of an existing Wayvia installation.

Prepare the exact pushed Git checkout, locked archives, wheel and uv first. Run
under a bounded systemd scope. This never enables new observers or notifications,
changes global routes/firewalls, or reboots a host. Only --apply stops services.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = Path("/var/lib/wayvia/control")
MANIFEST = Path("/var/lib/wayvia/install-manifest.json")
UNITS = (
    "wayvia-monitor.timer",
    "wayvia-monitor.service",
    "wayvia-certificate-renew.timer",
    "wayvia-usage.service",
    "wayvia-api.service",
    "wayvia-agent.service",
    "wayvia-ingress.service",
    "wayvia-bridge.service",
    "wayvia-carrier.service",
    "wayvia-vpn-client.service",
    "wayvia-vpn-server.service",
    "wayvia-exit.service",
    "wayvia-recover.service",
)
TREES = (
    "var/lib/wayvia",
    "etc/wayvia",
    "opt/wayvia/.venv",
    "opt/wayvia/source",
    "etc/systemd/system/wayvia-api.service.d",
)


def run(args, *, check=True, timeout=180):
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=timeout)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    data = value if isinstance(value, bytes) else (json.dumps(value, indent=2) + "\n").encode()
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def identity(root=STATE):
    with sqlite3.connect(f"file:{root / 'metadata.db'}?mode=ro", uri=True) as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        devices = c.execute(
            "SELECT id,name,platform,status,credential_version,secret_ref,desired_action,revision "
            "FROM devices ORDER BY id"
        ).fetchall()
        subscriptions = c.execute(
            "SELECT id,device_id,token_hash,format,expires_at,revoked_at,created_at "
            "FROM subscriptions ORDER BY id"
        ).fetchall()
        publications = c.execute("SELECT * FROM config_publications ORDER BY revision").fetchall()
        settings = c.execute(
            "SELECT * FROM settings WHERE key='published_revision' "
            "OR key LIKE 'subscription_title:%' "
            "ORDER BY key"
        ).fetchall()
        usage = (
            c.execute(
                "SELECT device_id,upload_bytes,download_bytes FROM device_usage ORDER BY device_id"
            ).fetchall()
            if "device_usage" in tables
            else []
        )
        schema = [r[0] for r in c.execute("SELECT version FROM schema_migrations ORDER BY version")]
    return {
        "identity_sha256": hashlib.sha256(
            json.dumps([devices, subscriptions, publications, settings]).encode()
        ).hexdigest(),
        "device_count": len(devices),
        "subscription_count": len(subscriptions),
        "usage": usage,
        "schema": schema,
        "control_config_sha256": sha(root / "config.yaml"),
        "control_secrets": {
            str(p.relative_to(root / "secrets")): sha(p)
            for p in (root / "secrets").rglob("*")
            if p.is_file()
        },
        "static_tls_secrets": {
            str(p): sha(p)
            for p in Path("/etc/wayvia/secrets").glob("*")
            if p.is_file() and p.suffix in {".crt", ".key", ".pem"}
        },
    }


def retained(before, after):
    if any(
        before[k] != after[k]
        for k in (
            "identity_sha256",
            "device_count",
            "subscription_count",
            "control_config_sha256",
            "control_secrets",
            "static_tls_secrets",
        )
    ):
        return False
    current = {r[0]: r[1:] for r in after["usage"]}
    return all(
        r[0] in current and all(b >= a for a, b in zip(r[1:], current[r[0]], strict=True))
        for r in before["usage"]
    )


def reduced_paths(paths):
    """Archive each tree once, using only explicit installation-owned paths."""
    result = []
    for path in sorted(set(paths), key=lambda p: (len(Path(p).parts), p)):
        if not any(Path(path).is_relative_to(Path(parent)) for parent in result):
            result.append(path)
    return result


def active(unit):
    return run(["systemctl", "is-active", unit], check=False).stdout.strip() in {
        "active",
        "activating",
        "reloading",
    }


def stop():
    for unit in UNITS:
        run(["systemctl", "stop", unit], check=False, timeout=90)
    if any(active(unit) for unit in UNITS):
        raise RuntimeError("Wayvia services did not stop")


def activate(previous):
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "reset-failed", *UNITS], check=False)
    for unit in (
        "wayvia-recover.service",
        "wayvia-agent.service",
        "wayvia-api.service",
        "wayvia-usage.service",
        "wayvia-monitor.timer",
        "wayvia-certificate-renew.timer",
    ):
        if previous.get(unit):
            action = "restart" if unit == "wayvia-recover.service" else "start"
            run(["systemctl", action, unit], timeout=180)


def ready(role, api_required):
    # Read the existing agent status. No new data-path probe is initiated here.
    code = (
        "import json; from wayvia.agent.client import AgentClient; "
        "rows=AgentClient(timeout=2).request({'operation':'status'}); "
        "rows=[r for r in rows if r.get('current') and r.get('role')==" + repr(role) + "]; "
        "assert len(rows)==1 and rows[0]['state']=='ACTIVE'; "
        "print(json.dumps({k:rows[0].get(k) for k in ['role','state','generation']}))"
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = run(
            ["runuser", "-u", "wayvia", "--", "/opt/wayvia/.venv/bin/python", "-c", code],
            check=False,
            timeout=5,
        )
        if status.returncode == 0:
            try:
                if api_required:
                    with opener.open("http://127.0.0.1:8787/healthz", timeout=2) as response:
                        if response.status != 200:
                            raise OSError("API not ready")
                return json.loads(status.stdout)
            except (OSError, ValueError):
                pass
        time.sleep(0.5)
    raise RuntimeError("Upgraded role or API did not become ready")


def preflight(args):
    commit = run(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).stdout.strip()
    if (
        commit != args.commit
        or run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"]).stdout
    ):
        raise RuntimeError("Expected a clean checkout at the exact pushed commit")
    manifest = json.loads(MANIFEST.read_text())
    if json.loads(Path("/etc/wayvia/manifest.json").read_text())["role"] != args.role:
        raise RuntimeError("Host role differs from the requested deployment role")
    if active("wayvia-certificate-renew.service"):
        raise RuntimeError("Wait for the existing certificate renewal to finish")
    if any(
        not (Path("/") / p).is_file() or sha(Path("/") / p) != v["sha256"]
        for p, v in manifest["files"].items()
    ):
        raise RuntimeError("Installation-owned file drift requires review")
    lock = json.loads((ROOT / "deploy/lab/components.lock.json").read_text())
    for component in lock["components"].values():
        asset = component["assets"][args.arch]
        if sha(args.components / asset["name"]) != asset["sha256"]:
            raise RuntimeError("Locked component archive mismatch")
    with zipfile.ZipFile(args.wheel) as wheel:
        for p in (ROOT / "src/wayvia").rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                if wheel.read(str(p.relative_to(ROOT / "src"))) != p.read_bytes():
                    raise RuntimeError("Wheel does not match the pushed source")
    if not run([str(args.uv), "--version"]).stdout.startswith("uv 0.9.7"):
        raise RuntimeError("Expected locked uv 0.9.7")
    if (
        os.geteuid() != 0
        or not active("wayvia-agent.service")
        or not active("wayvia-recover.service")
    ):
        raise RuntimeError("Expected the existing active root-managed Wayvia installation")
    return manifest


def upgrade(args):
    manifest = preflight(args)
    if not args.apply:
        return {"status": "prepared", "role": args.role, "commit": args.commit}
    previous = {unit: active(unit) for unit in UNITS}
    backup = Path("/var/backups/wayvia") / ("upgrade-" + args.commit[:12] + "-" + args.role)
    backup.mkdir(mode=0o700)
    write(backup / "active-units.json", previous)
    stopped = False
    archived = False
    try:
        stopped = True
        stop()
        before = identity()
        write(backup / "identity-before.json", before)
        paths = reduced_paths(
            [
                *TREES,
                *manifest["files"],
                *[str(p.relative_to("/")) for p in Path("/etc/systemd/system").glob("wayvia*")],
                "etc/nginx/conf.d/wayvia-https.conf",
            ]
        )
        paths = [p for p in paths if (Path("/") / p).exists()]
        write(backup / "archived-paths.json", paths)
        run(
            [
                "tar",
                "--numeric-owner",
                "--acls",
                "--xattrs",
                "-cpf",
                str(backup / "state.tar"),
                "-C",
                "/",
                *paths,
            ],
            timeout=300,
        )
        write(backup / "archive-sha256.json", {"sha256": sha(backup / "state.tar")})
        archived = True
        installed = run(
            [
                sys.executable,
                str(ROOT / "deploy/install.py"),
                "--system",
                "--arch",
                args.arch,
                "--uv",
                str(args.uv),
                "--components",
                str(args.components),
                "--wheel",
                str(args.wheel),
            ],
            timeout=300,
        )
        write(backup / "installer-result.json", json.loads(installed.stdout))
        dropins = Path("/etc/systemd/system/wayvia-api.service.d")
        (dropins / "50-subscription-client.conf").unlink(missing_ok=True)
        write(dropins / "40-runtime-limits.conf", b"[Service]\nMemoryHigh=384M\nMemoryMax=512M\n")
        nginx = Path("/etc/nginx/conf.d/wayvia-https.conf")
        nginx_changed = (
            args.role == "ingress"
            and nginx.exists()
            and b"proxy_set_header Host $host;" in nginx.read_bytes()
        )
        if nginx_changed:
            write(
                nginx,
                nginx.read_bytes().replace(
                    b"proxy_set_header Host $host;", b"proxy_set_header Host $http_host;"
                ),
            )
            run(["nginx", "-t"])
        run(
            [
                "runuser",
                "-u",
                "wayvia",
                "--",
                "/opt/wayvia/.venv/bin/python",
                "-c",
                "from pathlib import Path; from wayvia.storage import Database; "
                "Database(Path('/var/lib/wayvia/control/metadata.db'))",
            ]
        )
        after = identity()
        if after["schema"] != [1, 2, 3, 4] or not retained(before, after):
            raise RuntimeError("Migration did not preserve original data")
        write(backup / "identity-after-migration.json", after)
        activate(previous)
        readiness = ready(args.role, previous.get("wayvia-api.service", False))
        if nginx_changed:
            run(["systemctl", "reload", "nginx"])
        for unit, was_active in previous.items():
            if was_active and unit not in {"wayvia-monitor.service"} and not active(unit):
                raise RuntimeError("Previously active Wayvia service did not resume")
        if not retained(before, identity()):
            raise RuntimeError("Original identity or accumulated counters changed")
        result = {
            "status": "deployed",
            "role": args.role,
            "source_commit": args.commit,
            "wheel_sha256": sha(args.wheel),
            "storage_schema": 4,
            "original_data_preserved": True,
            "backup": str(backup),
            "new_observers_or_notifications_enabled": False,
            "global_routes_firewall_or_host_reboot": False,
            "readiness": readiness,
        }
        current = json.loads(MANIFEST.read_text())
        current["runtime_upgrade"] = result
        write(MANIFEST, current)
        write(backup / "result.json", result)
        return result
    except BaseException as exc:
        write(backup / "failure.json", {"type": type(exc).__name__})
        if isinstance(exc, subprocess.CalledProcessError):
            write(
                backup / "command-error.txt",
                ((exc.stdout or "") + "\n" + (exc.stderr or "")).encode(),
            )
        if archived:
            stop()
            failed = backup / "failed-new-state"
            for relative in [
                *TREES,
                *[str(p.relative_to("/")) for p in Path("/etc/systemd/system").glob("wayvia*")],
            ]:
                p = Path("/") / relative
                if p.exists():
                    dest = failed / relative
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(p), dest)
            run(
                [
                    "tar",
                    "--numeric-owner",
                    "--acls",
                    "--xattrs",
                    "-xpf",
                    str(backup / "state.tar"),
                    "-C",
                    "/",
                ],
                timeout=300,
            )
            activate(previous)
            nginx = Path("/etc/nginx/conf.d/wayvia-https.conf")
            if nginx.exists():
                run(["nginx", "-t"])
                run(["systemctl", "reload", "nginx"])
            write(backup / "rollback.json", {"status": "restored_current_preupgrade_snapshot"})
        elif stopped:
            activate(previous)
        raise


if __name__ == "__main__":

    def interrupted(signum, frame):
        raise InterruptedError("Upgrade interrupted; restore the current preupgrade snapshot")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=["exit", "ingress"], required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--arch", choices=["amd64", "arm64"], required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--components", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(upgrade(parser.parse_args())))
