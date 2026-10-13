#!/usr/bin/env python3
"""Prepare audited layouts or explicitly install on an authorized Ubuntu host.

Never starts services, changes routes, modifies firewalls, or downloads components.
Stage output is a reviewable filesystem layout, not a claim that Linux is running.
"""

from __future__ import annotations

import argparse
import fcntl
import grp
import gzip
import hashlib
import json
import os
import platform
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import uuid
import zipfile
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "var/lib/wayvia/install-manifest.json"
JOURNAL = "var/lib/wayvia/install-transaction.json"
UNITS = (
    "wayvia-api.service",
    "wayvia-agent.service",
    "wayvia-recover.service",
    "wayvia-monitor.service",
    "wayvia-monitor.timer",
    "wayvia-ingress.service",
    "wayvia-bridge.service",
    "wayvia-carrier.service",
    "wayvia-vpn-client.service",
    "wayvia-vpn-server.service",
    "wayvia-exit.service",
)
COMPONENTS = ("sing-box", "mihomo", "v2raya", "v2raya_core", "v2ray")


class InstallError(Exception):
    def __init__(self, code, message, exit_code=2):
        self.code, self.message, self.exit_code = code, message, exit_code


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(argv, *, env=None, check=True):
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, env=env, check=False, timeout=300
        )
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError(
            "command_unavailable", "Required installation command failed", 4
        ) from None
    if check and result.returncode:
        raise InstallError("command_failed", "Installation command returned nonzero", 5)
    return result


def checked_path(root: Path, relative: str) -> Path:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts:
        raise InstallError("invalid_owned_path", "Installation path is outside the owned layout")
    target = root.joinpath(*parts)
    # Refuse ancestor symlinks before creating files or traversing the target.
    for candidate in (root, *root.parents):
        if candidate.is_symlink():
            raise InstallError("symlink_refused", "Installation layout contains a symlink")
    cursor = root
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise InstallError("symlink_refused", "Installation layout contains a symlink")
    return target


def write(path: Path, data: bytes, mode: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".wayvia-install-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def wheel_input(source_root: Path, supplied: Path | None) -> tuple[Path, str]:
    if supplied is None:
        candidates = sorted((source_root / "dist").glob("wayvia-*-py3-none-any.whl"))
        if len(candidates) != 1:
            raise InstallError("wheel_required", "Build one wheel or provide --wheel explicitly")
        supplied = candidates[0]
    if supplied.is_symlink() or not supplied.is_file():
        raise InstallError("wheel_invalid", "Wheel must be a regular file")
    try:
        with zipfile.ZipFile(supplied) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1 or "wayvia/__init__.py" not in archive.namelist():
                raise InstallError("wheel_invalid", "Wheel must contain the Wayvia package")
            metadata = archive.read(metadata_names[0]).decode()
            if "\nName: wayvia\n" not in "\n" + metadata:
                raise InstallError("wheel_invalid", "Wheel metadata does not identify Wayvia")
            version = next(
                line[9:] for line in metadata.splitlines() if line.startswith("Version: ")
            )
            if not version or any(character in version for character in "/\\\n\x00"):
                raise InstallError("wheel_invalid", "Invalid package version")
    except (OSError, ValueError, zipfile.BadZipFile, StopIteration):
        raise InstallError("wheel_invalid", "Unable to read valid Wayvia wheel metadata") from None
    return supplied, version


def component_payloads(source_root: Path, component_dir: Path, arch: str) -> tuple[dict, dict]:
    lock_path = source_root / "deploy/lab/components.lock.json"
    lock = json.loads(lock_path.read_text())
    payloads, evidence = {}, {}
    for name in COMPONENTS:
        component = lock["components"][name]
        asset = component["assets"][arch]
        archive_path = component_dir / asset["name"]
        if archive_path.is_symlink() or not archive_path.is_file():
            raise InstallError(
                "component_missing", "Download all locked component archives first", 4
            )
        data = archive_path.read_bytes()
        if digest(data) != asset["sha256"]:
            raise InstallError("component_digest_mismatch", "Component archive checksum mismatch")
        try:
            if name == "sing-box":
                with tarfile.open(archive_path) as archive:
                    members = [
                        member
                        for member in archive.getmembers()
                        if member.name.endswith("/sing-box") and member.isfile()
                    ]
                    if len(members) != 1:
                        raise InstallError(
                            "component_archive_invalid", "Expected one sing-box binary"
                        )
                    binary = archive.extractfile(members[0]).read()
            elif name == "mihomo":
                binary = gzip.decompress(data)
            elif name == "v2ray":
                with zipfile.ZipFile(archive_path) as archive:
                    binary = archive.read("v2ray")
                    for filename in ("geoip.dat", "geosite.dat"):
                        payloads[f"usr/local/share/v2raya/{filename}"] = (
                            archive.read(filename),
                            0o644,
                        )
            else:
                binary = data
        except (OSError, ValueError, KeyError, tarfile.TarError, zipfile.BadZipFile):
            raise InstallError(
                "component_archive_invalid", "Locked component archive cannot be decoded"
            ) from None
        if not binary:
            raise InstallError("component_archive_invalid", "Component binary is empty")
        payloads[f"usr/local/bin/{name}"] = (binary, 0o755)
        evidence[name] = {
            "version": component["version"],
            "source": asset["url"],
            "archive_sha256": digest(data),
            "binary_sha256": digest(binary),
        }
    payloads["opt/wayvia/components.lock.json"] = (lock_path.read_bytes(), 0o644)
    return payloads, evidence


def api_unit() -> bytes:
    return rb"""[Unit]
Description=Wayvia local authenticated management and private subscriptions
After=wayvia-agent.service
Requires=wayvia-agent.service
[Service]
Type=simple
User=wayvia
Group=wayvia
ExecStart=/opt/wayvia/.venv/bin/wayvia \
  --system --mode production serve --host 127.0.0.1 --port 8787
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/wayvia/control
Restart=on-failure
[Install]
WantedBy=multi-user.target
"""


def existing_manifest(root: Path) -> dict | None:
    path = checked_path(root, MANIFEST)
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text())
        if manifest["schema_version"] != 1 or not isinstance(manifest["files"], dict):
            raise ValueError
        for relative, metadata in manifest["files"].items():
            if not owned_file(relative):
                raise InstallError(
                    "manifest_path_forbidden", "Manifest contains a nonowned file path"
                )
            checked_path(root, relative)
            if not isinstance(metadata.get("sha256"), str):
                raise ValueError
        return manifest
    except (ValueError, KeyError, OSError):
        raise InstallError(
            "manifest_invalid", "Previous installation manifest is invalid"
        ) from None


@contextmanager
def layout_lock(root: Path, *, operation=False):
    name = "system-operation.lock" if operation else "install.lock"
    path = checked_path(root, "var/lib/wayvia/" + name)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def remove_file(path: Path):
    path.unlink(missing_ok=True)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def recover_pending(root: Path) -> dict:
    """Undo an interrupted layout write, without trusting names as ownership.

    The journal is durable before the first payload write. An atomic manifest
    carrying its transaction ID is the commit point. Recovery itself is
    idempotent: every file can still contain either its before or after image.
    """
    path = checked_path(root, JOURNAL)
    if not path.exists():
        return {"status": "no_pending_installation"}
    try:
        journal = json.loads(path.read_text())
        if journal["schema_version"] != 1 or not isinstance(journal["files"], dict):
            raise ValueError
        manifest = existing_manifest(root)
        if manifest and manifest.get("transaction_id") == journal["id"]:
            remove_file(path)
            return {"status": "installation_already_committed"}
        restore = []
        for relative, change in journal["files"].items():
            if not owned_file(relative):
                raise ValueError
            target = checked_path(root, relative)
            before, after = change["before"], change["after_sha256"]
            current = digest(target.read_bytes()) if target.exists() else None
            expected = {None, after, before["sha256"] if before else None}
            if current not in expected:
                raise InstallError(
                    "recovery_file_modified",
                    "A pending installation file was modified; review it before recovery",
                    5,
                )
            data = None
            if before:
                saved = before["backup"]
                if not saved.startswith("var/backups/wayvia/"):
                    raise ValueError
                data = checked_path(root, saved).read_bytes()
                if digest(data) != before["sha256"]:
                    raise ValueError
            restore.append((target, before, data))
        # Validate all targets and backup hashes before mutating any of them.
        for target, before, data in reversed(restore):
            if before:
                write(target, data, before["mode"])
            elif target.exists():
                remove_file(target)
        remove_file(path)
        return {"status": "interrupted_installation_rolled_back", "files": len(restore)}
    except (ValueError, KeyError, TypeError, OSError):
        raise InstallError(
            "installation_recovery_required", "Pending installation journal or backup is invalid", 5
        ) from None


def recover_layout(root: Path, *, system=False) -> dict:
    if root == Path("/") and not system:
        raise InstallError("system_required", "Real filesystem recovery requires --system")
    with layout_lock(root):
        return recover_pending(root)


def backup(root: Path, previous: dict) -> str:
    identifier = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    relative = "var/backups/wayvia/" + identifier
    destination = checked_path(root, relative)
    destination.mkdir(parents=True, mode=0o700)
    sources = set(previous["files"]) | {MANIFEST}
    # Copy runtime inputs and journal state, but not temporary Unix sockets or venv.
    for directory in ("etc/wayvia", "var/lib/wayvia/control", "var/lib/wayvia/agent"):
        path = checked_path(root, directory)
        if path.exists():
            sources.update(
                str(item.relative_to(root)) for item in path.rglob("*") if item.is_file()
            )
    for relative_source in sorted(sources):
        if relative_source.endswith(("metadata.db-wal", "metadata.db-shm", "metadata.db-journal")):
            continue  # SQLite backup is consistent; do not attach stale sidecars.
        source = checked_path(root, relative_source)
        if not source.is_file():
            continue
        target = destination / relative_source
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.name == "metadata.db":
            with sqlite3.connect(source) as original, sqlite3.connect(target) as copied:
                original.backup(copied)
        else:
            shutil.copyfile(source, target)
        target.chmod(0o600)
    return relative


def prepare(
    root: Path,
    *,
    component_dir: Path,
    arch: str,
    wheel: Path | None = None,
    api_uid: int = 0,
    source_root: Path = ROOT,
) -> dict:
    if root.is_symlink() or root == Path("/"):
        raise InstallError("stage_invalid", "Staging requires a nonsymlink directory other than /")
    return install_layout(
        root,
        component_dir=component_dir,
        arch=arch,
        wheel=wheel,
        api_uid=api_uid,
        source_root=source_root,
        system=False,
    )


def install_layout(
    root: Path,
    *,
    component_dir: Path,
    arch: str,
    wheel: Path | None,
    api_uid: int,
    source_root: Path = ROOT,
    system=False,
) -> dict:
    selected_wheel, version = wheel_input(source_root, wheel)
    payloads, components = component_payloads(source_root, component_dir, arch)
    payloads[f"opt/wayvia/wayvia-{version}-py3-none-any.whl"] = (selected_wheel.read_bytes(), 0o644)
    for filename in ("pyproject.toml", "uv.lock"):
        payloads["opt/wayvia/source/" + filename] = ((source_root / filename).read_bytes(), 0o644)
    for source in sorted((source_root / "src/wayvia").rglob("*")):
        if source.is_symlink():
            raise InstallError("source_symlink", "Package source cannot contain symlinks")
        if source.is_file() and "__pycache__" not in source.parts:
            relative = source.relative_to(source_root)
            payloads["opt/wayvia/source/" + str(relative)] = (source.read_bytes(), 0o644)
    for filename in (
        "wayvia-agent.service",
        "wayvia-recover.service",
        "wayvia-monitor.service",
        "wayvia-monitor.timer",
    ):
        data = (source_root / "deploy/systemd" / filename).read_bytes()
        payloads["etc/systemd/system/" + filename] = (data, 0o644)
    payloads["etc/systemd/system/wayvia-api.service"] = (api_unit(), 0o644)
    payloads["etc/wayvia/agent.env"] = (f"WAYVIA_API_UID={api_uid}\n".encode(), 0o600)
    payloads["etc/tmpfiles.d/wayvia.conf"] = (
        b"d /run/wayvia 0750 root wayvia -\nd /var/lib/wayvia 0750 root wayvia -\n",
        0o644,
    )
    with layout_lock(root):
        recover_pending(root)
        return write_layout(root, payloads, components, version, arch, api_uid, system)


def write_layout(root, payloads, components, version, arch, api_uid, system):
    previous = existing_manifest(root)
    # A matching unowned filename is still a conflict; never infer ownership from names.
    for relative in payloads:
        target = checked_path(root, relative)
        if target.exists() and (previous is None or relative not in previous["files"]):
            raise InstallError("resource_conflict", "A target file is not owned by this installer")
        if target.exists() and digest(target.read_bytes()) != previous["files"][relative]["sha256"]:
            raise InstallError(
                "owned_file_modified", "An owned file changed; review it before upgrade"
            )
    obsolete = set(previous["files"] if previous else ()) - set(payloads)
    retained, removed = {}, []
    for relative in sorted(obsolete):
        target = checked_path(root, relative)
        if not target.exists():
            continue
        if digest(target.read_bytes()) == previous["files"][relative]["sha256"]:
            removed.append(relative)
        else:
            # Keep the original hash: uninstall must still recognize this as an
            # operator modification instead of silently taking ownership of it.
            retained[relative] = previous["files"][relative]
    backup_path = backup(root, previous) if previous else None
    for relative, mode in {
        "etc/wayvia": 0o700,
        "etc/wayvia/secrets": 0o700,
        "var/lib/wayvia": 0o750,
        "var/lib/wayvia/control": 0o700,
        "var/lib/wayvia/agent": 0o700,
        "run/wayvia": 0o750,
    }.items():
        directory = checked_path(root, relative)
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(mode)
    changes = {}
    for relative in [*payloads, *removed]:
        target = checked_path(root, relative)
        changes[relative] = {
            "before": {
                "sha256": digest(target.read_bytes()),
                "mode": target.stat().st_mode & 0o777,
                "backup": backup_path + "/" + relative,
            }
            if target.exists()
            else None,
            "after_sha256": digest(payloads[relative][0]) if relative in payloads else None,
        }
    transaction_id = uuid.uuid4().hex
    journal = {"schema_version": 1, "id": transaction_id, "files": changes}
    write(checked_path(root, JOURNAL), (json.dumps(journal, indent=2) + "\n").encode(), 0o600)
    for relative, (data, mode) in payloads.items():
        write(checked_path(root, relative), data, mode)
    for relative in removed:
        remove_file(checked_path(root, relative))
    manifest = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "version": version,
        "architecture": arch,
        "created_at": datetime.now(UTC).isoformat(),
        "api_uid": api_uid,
        "status": "runtime_install_pending" if system else "prepared",
        "runtime": "/opt/wayvia/.venv",
        "wheel": f"/opt/wayvia/wayvia-{version}-py3-none-any.whl",
        "backup": backup_path,
        "components": components,
        "services_started": False,
        "network_changed": False,
        "obsolete_files_removed": removed,
        "modified_obsolete_files_retained": sorted(retained),
        "files": {
            **retained,
            **{
                relative: {"sha256": digest(data), "mode": mode}
                for relative, (data, mode) in payloads.items()
            },
        },
    }
    write(checked_path(root, MANIFEST), (json.dumps(manifest, indent=2) + "\n").encode(), 0o600)
    remove_file(checked_path(root, JOURNAL))
    return {
        "status": manifest["status"],
        "version": version,
        "root": str(root),
        "manifest": str(root / MANIFEST),
        "backup": backup_path,
        "obsolete_files_removed": removed,
        "modified_obsolete_files_retained": sorted(retained),
        "components": "locked_archive_checksums_verified",
        "services_started": False,
        "network_changed": False,
        "runtime": "requires_target_python_3.12_install",
    }


def system_preflight(uv: str, *, install_runtime=True):
    if platform.system() != "Linux" or os.geteuid() != 0:
        raise InstallError(
            "system_environment_required", "--system requires an authorized Linux root host", 4
        )
    if install_runtime:
        release = platform.freedesktop_os_release()
        if release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04":
            raise InstallError("system_version_unsupported", "System baseline is Ubuntu 24.04", 4)
        if not run([uv, "--version"]).stdout.startswith("uv 0.9.7"):
            raise InstallError(
                "uv_version_mismatch", "Install uv 0.9.7 before system installation", 4
            )
        if not run(["openvpn", "--version"]).stdout.startswith("OpenVPN 2.6.19"):
            raise InstallError("openvpn_version_mismatch", "Install locked OpenVPN 2.6.19 first", 4)
    commands = (
        ("systemctl", "ip", "nft", "nc", "dig", "curl", "openssl", "python3.12")
        if install_runtime
        else ("systemctl",)
    )
    for command in commands:
        if not shutil.which(command):
            raise InstallError(
                "system_prerequisite_missing", "Required Linux tools are unavailable", 4
            )
    run(["systemctl", "show", "--property=Version", "--value"])
    for unit in UNITS:
        if run(["systemctl", "is-active", unit], check=False).stdout.strip() in {
            "active",
            "activating",
            "reloading",
        }:
            raise InstallError(
                "services_active", "Stop all Wayvia units before installing or uninstalling", 5
            )


def service_account(root: Path = Path("/")):
    """Persist a reserved group identity before account commands may be interrupted."""
    with layout_lock(root):
        intent_path = checked_path(root, "var/lib/wayvia/account-intent.json")
        intent = json.loads(intent_path.read_text()) if intent_path.exists() else None
        if intent is not None and (
            intent.get("schema_version") != 1
            or not isinstance(intent.get("gid"), int)
            or not 100 <= intent["gid"] < 1000
        ):
            raise InstallError("account_recovery_required", "Account creation record is invalid")
        try:
            group = grp.getgrnam("wayvia")
        except KeyError:
            group = None
        try:
            user = pwd.getpwnam("wayvia")
        except KeyError:
            user = None
        if user is not None:
            if (
                user.pw_uid == 0
                or user.pw_dir != "/var/lib/wayvia/control"
                or user.pw_shell != "/usr/sbin/nologin"
                or group is None
                or group.gr_gid == 0
                or user.pw_gid != group.gr_gid
                or (intent is not None and group.gr_gid != intent["gid"])
            ):
                raise InstallError(
                    "user_conflict", "Existing wayvia account has unexpected identity"
                )
            return user
        if group is not None and (intent is None or group.gr_gid != intent["gid"] or group.gr_mem):
            raise InstallError(
                "group_conflict", "Existing wayvia group is not owned by this installation"
            )
        if intent is None:
            used = {item.gr_gid for item in grp.getgrall()}
            gid = next((value for value in range(999, 99, -1) if value not in used), None)
            if gid is None:
                raise InstallError(
                    "group_unavailable", "No unused system group identifier is available"
                )
            intent = {"schema_version": 1, "gid": gid}
            write(intent_path, (json.dumps(intent) + "\n").encode(), 0o600)
        if group is None:
            try:
                grp.getgrgid(intent["gid"])
            except KeyError:
                pass
            else:
                raise InstallError(
                    "group_conflict", "Reserved group identity belongs to another group"
                )
            run(["groupadd", "--system", "--gid", str(intent["gid"]), "wayvia"])
        run(
            [
                "useradd",
                "--system",
                "--gid",
                "wayvia",
                "--home-dir",
                "/var/lib/wayvia/control",
                "--shell",
                "/usr/sbin/nologin",
                "wayvia",
            ]
        )
        user = pwd.getpwnam("wayvia")
        if user.pw_gid != intent["gid"] or user.pw_uid == 0:
            raise InstallError("user_conflict", "Created account does not match its reserved group")
        return user


def system_install(args) -> dict:
    system_preflight(args.uv)
    actual_arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine())
    if args.arch != actual_arch:
        raise InstallError(
            "architecture_mismatch", "Component architecture differs from this host", 4
        )
    # Validate payloads before changing accounts or installation files.
    wheel_input(ROOT, args.wheel)
    component_payloads(ROOT, args.components, args.arch)
    user = service_account()
    result = install_layout(
        Path("/"),
        component_dir=args.components,
        arch=args.arch,
        wheel=args.wheel,
        api_uid=user.pw_uid,
        system=True,
    )
    installed_manifest = json.loads((Path("/") / MANIFEST).read_text())
    env = os.environ.copy()
    env["UV_PROJECT_ENVIRONMENT"] = "/opt/wayvia/.venv"
    env["UV_PYTHON_DOWNLOADS"] = "never"
    run(
        [
            args.uv,
            "sync",
            "--locked",
            "--no-dev",
            "--no-install-project",
            "--project",
            "/opt/wayvia/source",
            "--python",
            "python3.12",
        ],
        env=env,
    )
    run(
        [
            args.uv,
            "pip",
            "install",
            "--no-deps",
            "--python",
            "/opt/wayvia/.venv/bin/python",
            installed_manifest["wheel"],
        ]
    )
    run(["/opt/wayvia/.venv/bin/wayvia", "--version"])
    control = Path("/var/lib/wayvia/control")
    if not (control / "config.yaml").exists():
        run(["/opt/wayvia/.venv/bin/wayvia", "--data-dir", str(control), "init"])
    for path in (control, *control.rglob("*")):
        if path.is_symlink():
            raise InstallError("symlink_refused", "Control state contains a symlink")
        os.chown(path, user.pw_uid, user.pw_gid)
    for path in (Path("/var/lib/wayvia"), Path("/run/wayvia")):
        os.chown(path, 0, user.pw_gid)
    run(["systemctl", "daemon-reload"])
    path = Path("/") / MANIFEST
    manifest = json.loads(path.read_text())
    manifest["status"] = "installed_inactive"
    write(path, (json.dumps(manifest, indent=2) + "\n").encode(), 0o600)
    return {
        **result,
        "status": "installed_inactive",
        "runtime": "locked_venv_installed",
        "api_uid": user.pw_uid,
        "activation_required": True,
    }


def owned_file(relative: str) -> bool:
    path = Path(relative)
    return (
        (
            relative in {f"usr/local/bin/{component}" for component in COMPONENTS}
            or relative in {"etc/wayvia/agent.env", "etc/tmpfiles.d/wayvia.conf"}
            or relative
            in {"usr/local/share/v2raya/geoip.dat", "usr/local/share/v2raya/geosite.dat"}
            or relative.startswith("opt/wayvia/")
            or (str(path.parent) == "etc/systemd/system" and path.name in UNITS)
        )
        and ".." not in path.parts
        and not path.is_absolute()
    )


def uninstall(root: Path, *, system=False) -> dict:
    if root == Path("/") and not system:
        raise InstallError("system_required", "Real filesystem uninstall requires --system")
    with layout_lock(root):
        recovered = recover_pending(root)
        if existing_manifest(root) is None and recovered["status"] != "no_pending_installation":
            return {
                **recovered,
                "status": "uninstalled_interrupted_layout",
                "network_changed": False,
            }
        return uninstall_layout(root, system=system)


def uninstall_layout(root: Path, *, system=False) -> dict:
    manifest = existing_manifest(root)
    if manifest is None:
        raise InstallError("not_installed", "No owned installation manifest was found")
    for relative in manifest["files"]:
        if not owned_file(relative):
            raise InstallError("manifest_path_forbidden", "Manifest contains a nonowned file path")
        checked_path(root, relative)
    backup_path = backup(root, manifest)
    removed, retained = [], []
    for relative, metadata in manifest["files"].items():
        path = checked_path(root, relative)
        if not path.exists():
            continue
        if digest(path.read_bytes()) != metadata["sha256"]:
            retained.append(relative)
        else:
            path.unlink()
            removed.append(relative)
    # Preserve control SQLite, credentials, agent journals, venv and account.
    manifest["status"] = "uninstalled_data_retained"
    manifest["files"] = {relative: manifest["files"][relative] for relative in retained}
    manifest["backup"] = backup_path
    write(checked_path(root, MANIFEST), (json.dumps(manifest, indent=2) + "\n").encode(), 0o600)
    if system:
        run(["systemctl", "daemon-reload"])
    return {
        "status": manifest["status"],
        "removed_files": len(removed),
        "modified_files_retained": retained,
        "runtime_data_retained": True,
        "venv_and_user_retained": True,
        "backup": backup_path,
        "services_started": False,
        "network_changed": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument(
        "--stage", type=Path, help="prepare a filesystem layout without activation"
    )
    destination.add_argument(
        "--system", action="store_true", help="explicit authorized Ubuntu root installation"
    )
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--uninstall",
        action="store_true",
        help="remove unchanged owned files; keep all runtime data",
    )
    operation.add_argument(
        "--recover", action="store_true", help="roll back an interrupted installation layout"
    )
    parser.add_argument(
        "--arch",
        choices=("arm64", "amd64"),
        default="arm64" if platform.machine() in {"arm64", "aarch64"} else "amd64",
    )
    parser.add_argument("--components", type=Path)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument(
        "--api-uid",
        type=int,
        default=0,
        help="UID shown in stage agent.env; actual system account is resolved automatically",
    )
    parser.add_argument("--uv", default="uv", help="path to locked uv 0.9.7")
    args = parser.parse_args(argv)
    args.components = args.components or ROOT / ".cache/components" / args.arch
    try:
        if args.api_uid < 0:
            raise InstallError("uid_invalid", "API UID must be nonnegative")
        if args.system and (platform.system() != "Linux" or os.geteuid() != 0):
            raise InstallError("system_environment", "System installation requires Linux root", 4)
        # Serialize the whole public system lifecycle, including account commands,
        # dependency installation and manifest completion; layout operations nest
        # on a different lock so they remain reusable for unprivileged staging.
        with layout_lock(Path("/"), operation=True) if args.system else nullcontext():
            if args.recover:
                if args.system:
                    system_preflight(args.uv, install_runtime=False)
                result = recover_layout(
                    Path("/") if args.system else args.stage.absolute(), system=args.system
                )
            elif args.uninstall:
                if args.system:
                    system_preflight(args.uv, install_runtime=False)
                result = uninstall(
                    Path("/") if args.system else args.stage.absolute(), system=args.system
                )
            elif args.system:
                result = system_install(args)
            else:
                result = prepare(
                    args.stage.absolute(),
                    component_dir=args.components,
                    arch=args.arch,
                    wheel=args.wheel,
                    api_uid=args.api_uid,
                )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except InstallError as error:
        print(json.dumps({"code": error.code, "message": error.message}), file=sys.stderr)
        return error.exit_code
    except (OSError, ValueError, KeyError):
        print(
            json.dumps(
                {
                    "code": "installation_failed",
                    "message": "Installation inputs or filesystem could not be processed",
                }
            ),
            file=sys.stderr,
        )
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
