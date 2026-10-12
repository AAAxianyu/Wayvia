"""Installer safety tests use explicit isolated archive fixtures, never the host layout."""

import gzip
import hashlib
import importlib.util
import io
import json
import platform
import sqlite3
import tarfile
import zipfile
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "deploy/install.py"
SPEC = importlib.util.spec_from_file_location("wayvia_install_test", MODULE_PATH)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


@pytest.fixture
def inputs(tmp_path):
    source = tmp_path / "source"
    cache = tmp_path / "components"
    cache.mkdir()
    (source / "deploy/lab").mkdir(parents=True)
    (source / "deploy/systemd").mkdir()
    (source / "src/wayvia").mkdir(parents=True)
    (source / "src/wayvia/__init__.py").write_text('__version__ = "0.1.0"\n')
    (source / "pyproject.toml").write_text('[project]\nname = "wayvia"\nversion = "0.1.0"\n')
    (source / "uv.lock").write_text("version = 1\n")
    for unit in (
        "wayvia-agent.service",
        "wayvia-recover.service",
        "wayvia-monitor.service",
        "wayvia-monitor.timer",
    ):
        (source / "deploy/systemd" / unit).write_text("[Unit]\nDescription=Isolated test fixture\n")
    lock = {"schema_version": 1, "components": {}}
    for component in installer.COMPONENTS:
        data = b"explicit-isolated-binary-fixture:" + component.encode()
        filename = component + ".asset"
        if component == "sing-box":
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                member = tarfile.TarInfo("release/sing-box")
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            data = buffer.getvalue()
        elif component == "mihomo":
            data = gzip.compress(data)
        elif component == "v2ray":
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("v2ray", data)
                archive.writestr("geoip.dat", b"fixture-geoip")
                archive.writestr("geosite.dat", b"fixture-geosite")
            data = buffer.getvalue()
        (cache / filename).write_bytes(data)
        lock["components"][component] = {
            "version": "test-fixture",
            "assets": {
                "arm64": {
                    "name": filename,
                    "url": "https://fixture.invalid/" + filename,
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            },
        }
    (source / "deploy/lab/components.lock.json").write_text(json.dumps(lock))
    wheel = tmp_path / "wayvia-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("wayvia/__init__.py", '__version__ = "0.1.0"')
        archive.writestr(
            "wayvia-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: wayvia\nVersion: 0.1.0\n",
        )
    return source, cache, wheel


def prepare(root, inputs):
    source, cache, wheel = inputs
    return installer.prepare(
        root, component_dir=cache, arch="arm64", wheel=wheel, api_uid=20001, source_root=source
    )


def test_nonprivileged_stage_is_complete_inactive_and_has_restricted_permissions(tmp_path, inputs):
    stage = tmp_path / "stage"
    result = prepare(stage, inputs)
    assert result["status"] == "prepared"
    assert result["services_started"] is False
    assert result["network_changed"] is False
    assert result["runtime"] == "requires_target_python_3.12_install"
    manifest = json.loads((stage / installer.MANIFEST).read_text())
    assert manifest["components"]["sing-box"]["archive_sha256"]
    assert (stage / "usr/local/bin/sing-box").stat().st_mode & 0o777 == 0o755
    assert (stage / "etc/wayvia/secrets").stat().st_mode & 0o777 == 0o700
    assert (stage / "etc/wayvia/agent.env").stat().st_mode & 0o777 == 0o600
    assert (stage / "etc/wayvia/agent.env").read_text() == "WAYVIA_API_UID=20001\n"
    unit = (stage / "etc/systemd/system/wayvia-api.service").read_text()
    assert "User=wayvia" in unit
    assert "--host 127.0.0.1" in unit
    assert "--mode production" in unit
    assert (stage / "opt/wayvia/wayvia-0.1.0-py3-none-any.whl").is_file()
    assert manifest["wheel"] == "/opt/wayvia/wayvia-0.1.0-py3-none-any.whl"
    assert (stage / "opt/wayvia/source/uv.lock").is_file()
    assert not (stage / "opt/wayvia/.venv").exists()


def test_cached_binary_tampering_cannot_override_locked_archive(tmp_path, inputs):
    _, cache, _ = inputs
    (cache / "sing-box").write_bytes(b"malicious-unlocked-binary")
    stage = tmp_path / "stage"
    prepare(stage, inputs)
    assert (stage / "usr/local/bin/sing-box").read_bytes().startswith(b"explicit-isolated-binary")


def test_archive_digest_mismatch_fails_before_layout_mutation(tmp_path, inputs):
    _, cache, _ = inputs
    (cache / "sing-box.asset").write_bytes(b"tampered")
    stage = tmp_path / "stage"
    with pytest.raises(installer.InstallError) as failure:
        prepare(stage, inputs)
    assert failure.value.code == "component_digest_mismatch"
    assert not stage.exists()


def test_unowned_file_or_symlink_is_never_overwritten(tmp_path, inputs):
    stage = tmp_path / "stage"
    target = stage / "usr/local/bin/sing-box"
    target.parent.mkdir(parents=True)
    target.write_text("existing-unrelated-service")
    with pytest.raises(installer.InstallError, match="not owned"):
        prepare(stage, inputs)
    assert target.read_text() == "existing-unrelated-service"
    target.unlink()
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    target.symlink_to(outside)
    with pytest.raises(installer.InstallError, match="symlink"):
        prepare(stage, inputs)
    assert outside.read_text() == "untouched"


def test_upgrade_backs_up_owned_inputs_and_uninstall_retains_runtime_data(tmp_path, inputs):
    stage = tmp_path / "stage"
    prepare(stage, inputs)
    state = stage / "var/lib/wayvia/control/secret.txt"
    state.write_text("isolated-sensitive-fixture")
    database = state.parent / "metadata.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE example(value TEXT)")
        connection.execute("INSERT INTO example VALUES('consistent-snapshot')")
    (state.parent / "metadata.db-wal").write_text("stale-sidecar")
    result = prepare(stage, inputs)
    backup = stage / result["backup"]
    assert (backup / "usr/local/bin/sing-box").is_file()
    assert (backup / "var/lib/wayvia/control/secret.txt").read_text() == state.read_text()
    with sqlite3.connect(backup / "var/lib/wayvia/control/metadata.db") as connection:
        assert (
            connection.execute("SELECT value FROM example").fetchone()[0] == "consistent-snapshot"
        )
    assert not (backup / "var/lib/wayvia/control/metadata.db-wal").exists()
    uninstalled = installer.uninstall(stage)
    assert uninstalled["runtime_data_retained"]
    assert state.exists()
    assert not (stage / "usr/local/bin/sing-box").exists()
    assert not (stage / "etc/systemd/system/wayvia-api.service").exists()
    assert not uninstalled["network_changed"]


def test_modified_owned_files_stop_upgrade_and_survive_uninstall(tmp_path, inputs):
    stage = tmp_path / "stage"
    prepare(stage, inputs)
    unit = stage / "etc/systemd/system/wayvia-api.service"
    unit.write_text("operator-modified-unit")
    with pytest.raises(installer.InstallError) as failure:
        prepare(stage, inputs)
    assert failure.value.code == "owned_file_modified"
    result = installer.uninstall(stage)
    assert "etc/systemd/system/wayvia-api.service" in result["modified_files_retained"]
    assert unit.read_text() == "operator-modified-unit"


def test_tampered_uninstall_manifest_cannot_delete_outside_owned_layout(tmp_path, inputs):
    stage = tmp_path / "stage"
    prepare(stage, inputs)
    outside = stage / "etc/unrelated.conf"
    outside.write_text("untouched")
    manifest_path = stage / installer.MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["etc/unrelated.conf"] = {
        "sha256": hashlib.sha256(outside.read_bytes()).hexdigest()
    }
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(installer.InstallError) as failure:
        installer.uninstall(stage)
    assert failure.value.code == "manifest_path_forbidden"
    assert outside.read_text() == "untouched"


def test_system_mode_is_rejected_without_supported_linux_root(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    with pytest.raises(installer.InstallError) as failure:
        installer.system_preflight("uv")
    assert failure.value.code == "system_environment_required"
    assert failure.value.exit_code == 4


def test_stage_cannot_escape_through_a_symlink_parent(tmp_path, inputs):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(installer.InstallError) as failure:
        prepare(alias / "stage", inputs)
    assert failure.value.code == "symlink_refused"
    assert not (actual / "stage").exists()


def test_prepare_never_invokes_system_commands(tmp_path, inputs, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("prepare must not execute commands")

    monkeypatch.setattr(installer, "run", forbidden)
    prepare(tmp_path / "stage", inputs)
