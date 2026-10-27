#!/usr/bin/env python3
"""Bound Wayvia subscription traffic without touching other nginx virtual hosts."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

CONFIG = Path("/etc/nginx/conf.d/wayvia-https.conf")
BACKUPS = Path("/var/backups/wayvia")
LOCK = Path("/run/lock/wayvia-subscription-limits.lock")
ANCHOR = b"    location ^~ /subscriptions/ {\n"
ZONES = (
    b"limit_req_zone $binary_remote_addr zone=wayvia_sub_rate:1m rate=5r/m;\n"
    b"limit_conn_zone $server_name zone=wayvia_sub_concurrent:1m;\n\n"
)
LIMITS = (
    b"        limit_req zone=wayvia_sub_rate burst=5 nodelay;\n"
    b"        limit_req_status 429;\n"
    b"        limit_conn wayvia_sub_concurrent 2;\n"
    b"        limit_conn_status 429;\n"
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def render(original):
    if original.count(ANCHOR) != 1 or not original.startswith(b"server {\n"):
        raise ValueError("Unexpected nginx configuration layout")
    if b"wayvia_sub_rate" in original or b"wayvia_sub_concurrent" in original:
        raise ValueError("Subscription limits already exist")
    return ZONES + original.replace(ANCHOR, ANCHOR + LIMITS, 1)


def read_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
            raise ValueError("Expected a bounded regular configuration file")
        return source.read(), metadata


def replace(path, content, metadata):
    fd, name = tempfile.mkstemp(prefix=".wayvia-sub-limits-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            os.fchmod(target.fileno(), stat.S_IMODE(metadata.st_mode))
            os.fchown(target.fileno(), metadata.st_uid, metadata.st_gid)
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
        dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run(*argv):
    return subprocess.run(
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30
    ).returncode


def check_and_reload():
    if run("nginx", "-t"):
        raise RuntimeError("nginx configuration validation failed")
    if run("systemctl", "reload", "nginx.service"):
        raise RuntimeError("nginx reload failed")
    if run("systemctl", "is-active", "--quiet", "nginx.service"):
        raise RuntimeError("nginx is not active after reload")


def apply(expected_original, expected_candidate):
    original, metadata = read_regular(CONFIG)
    candidate = render(original)
    if sha(original) != expected_original or sha(candidate) != expected_candidate:
        raise ValueError("Configuration or candidate differs from the verified baseline")
    dump = subprocess.run(
        ["nginx", "-T"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30
    )
    if (
        dump.returncode
        or b"zone=wayvia_sub_rate" in dump.stdout
        or b"zone=wayvia_sub_concurrent" in dump.stdout
    ):
        raise ValueError("nginx configuration is invalid or a limit zone already exists")
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="subscription-limits-", dir=BACKUPS))
    (backup / "original.conf").write_bytes(original)
    (backup / "manifest.json").write_text(
        json.dumps({"original_sha256": sha(original), "candidate_sha256": sha(candidate)})
    )
    if sha(read_regular(CONFIG)[0]) != expected_original:
        raise ValueError("Configuration changed during preflight")
    replace(CONFIG, candidate, metadata)
    try:
        check_and_reload()
    except Exception:
        # Never overwrite a concurrent change during emergency restoration.
        if sha(read_regular(CONFIG)[0]) == sha(candidate):
            replace(CONFIG, original, metadata)
            check_and_reload()
        raise
    return {"status": "applied", "backup": str(backup), "candidate_sha256": sha(candidate)}


def restore(backup):
    backup = Path(backup).resolve(strict=True)
    if backup.parent != BACKUPS.resolve() or not backup.name.startswith("subscription-limits-"):
        raise ValueError("Invalid backup directory")
    manifest = json.loads((backup / "manifest.json").read_text())
    original, _ = read_regular(backup / "original.conf")
    current, metadata = read_regular(CONFIG)
    if sha(original) != manifest["original_sha256"] or sha(current) != manifest["candidate_sha256"]:
        raise ValueError("Backup or live configuration changed; manual review required")
    replace(CONFIG, original, metadata)
    check_and_reload()
    return {"status": "restored", "original_sha256": sha(original)}


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--restore", metavar="BACKUP")
    parser.add_argument("--expected-original")
    parser.add_argument("--expected-candidate")
    args = parser.parse_args()
    if args.apply and not all(
        re.fullmatch(r"[0-9a-f]{64}", value or "")
        for value in (args.expected_original, args.expected_candidate)
    ):
        parser.error("--apply requires both verified SHA256 values")
    os.umask(0o077)
    try:
        with LOCK.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = (
                apply(args.expected_original, args.expected_candidate)
                if args.apply
                else restore(args.restore)
            )
        print(json.dumps(result))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"status": "failed", "reason": str(error)}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
