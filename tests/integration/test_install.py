"""Installer safety tests use explicit isolated archive fixtures, never the host layout."""

import fcntl
import gzip
import hashlib
import importlib.util
import io
import json
import os
import platform
import sqlite3
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "deploy/install.py"
SPEC = importlib.util.spec_from_file_location("wayvia_install_test", MODULE_PATH)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


@pytest.mark.parametrize("operation", ["install", "uninstall", "recover"])
def test_system_entrypoint_holds_whole_operation_lock(tmp_path, monkeypatch, operation):
    original_lock = installer.layout_lock
    monkeypatch.setattr(
        installer, "layout_lock", lambda root, **kwargs: original_lock(tmp_path, **kwargs)
    )
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer, "system_preflight", lambda *args, **kwargs: None)

    def execute(*args, **kwargs):
        with (tmp_path / "var/lib/wayvia/system-operation.lock").open("r+") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with original_lock(tmp_path):
            return {"status": "isolated_lifecycle_fixture"}

    for function in ("system_install", "uninstall", "recover_layout"):
        monkeypatch.setattr(installer, function, execute)
    assert (
        installer.main(["--system", *([] if operation == "install" else ["--" + operation])]) == 0
    )


@pytest.mark.parametrize(
    "interruption", ["before_group", "after_group", "before_user", "after_user"]
)
def test_service_account_retries_interrupted_creation_without_adopting_another_group(
    tmp_path, monkeypatch, interruption
):
    accounts = {}
    calls = []

    def lookup(key):
        if key not in accounts:
            raise KeyError(key)
        return accounts[key]

    monkeypatch.setattr(installer.pwd, "getpwnam", lambda name: lookup("user"))
    monkeypatch.setattr(installer.grp, "getgrnam", lambda name: lookup("group"))
    monkeypatch.setattr(installer.grp, "getgrgid", lambda gid: lookup("group"))
    monkeypatch.setattr(installer.grp, "getgrall", lambda: [])

    def invoke(argv):
        nonlocal interruption
        stage = "group" if argv[0] == "groupadd" else "user"
        if interruption == "before_" + stage:
            interruption = None
            raise InterruptedError("isolated account command interruption")
        calls.append(argv[0])
        if stage == "group":
            accounts["group"] = SimpleNamespace(gr_gid=int(argv[3]), gr_mem=[])
        else:
            accounts["user"] = SimpleNamespace(
                pw_uid=901,
                pw_gid=accounts["group"].gr_gid,
                pw_dir="/var/lib/wayvia/control",
                pw_shell="/usr/sbin/nologin",
            )
        if interruption == "after_" + stage:
            interruption = None
            raise InterruptedError("isolated account command interruption")

    monkeypatch.setattr(installer, "run", invoke)
    with pytest.raises(InterruptedError):
        installer.service_account(tmp_path)
    user = installer.service_account(tmp_path)
    assert user.pw_uid == 901
    assert calls == ["groupadd", "useradd"]
    assert (tmp_path / "var/lib/wayvia/account-intent.json").stat().st_mode & 0o777 == 0o600


def test_service_account_refuses_unowned_preexisting_group(tmp_path, monkeypatch):
    def absent(name):
        raise KeyError(name)

    monkeypatch.setattr(installer.pwd, "getpwnam", absent)
    monkeypatch.setattr(
        installer.grp, "getgrnam", lambda name: SimpleNamespace(gr_gid=901, gr_mem=[])
    )
    monkeypatch.setattr(
        installer, "run", lambda argv: pytest.fail("must not change an unowned group")
    )
    with pytest.raises(installer.InstallError) as failure:
        installer.service_account(tmp_path)
    assert failure.value.code == "group_conflict"


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
    assert "--system" in unit
    assert (stage / "opt/wayvia/wayvia-0.1.0-py3-none-any.whl").is_file()
    assert manifest["wheel"] == "/opt/wayvia/wayvia-0.1.0-py3-none-any.whl"
    assert (stage / "opt/wayvia/source/uv.lock").is_file()
    assert not (stage / "opt/wayvia/.venv").exists()


def test_strict_umask_keeps_public_runtime_parent_and_private_state_distinct(tmp_path, inputs):
    before = os.umask(0o077)
    try:
        stage = tmp_path / "strict-stage"
        prepare(stage, inputs)
        assert (stage / "opt/wayvia").stat().st_mode & 0o777 == 0o755
        assert (stage / "etc/wayvia/secrets").stat().st_mode & 0o777 == 0o700
        assert (stage / "etc/wayvia/agent.env").stat().st_mode & 0o777 == 0o600
        public = tmp_path / "public-runtime-file"
        installer.run(
            [
                sys.executable,
                "-c",
                "import pathlib,sys;pathlib.Path(sys.argv[1]).touch()",
                str(public),
            ],
            umask=0o022,
        )
        private = tmp_path / "private-after-runtime"
        private.touch()
        assert public.stat().st_mode & 0o777 == 0o644
        assert private.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(before)


def test_runtime_install_requires_service_identity_access(monkeypatch):
    def invoke(argv, **kwargs):
        return SimpleNamespace(returncode=1 if argv[0] == "runuser" else 0, stdout="")

    monkeypatch.setattr(installer, "run", invoke)
    with pytest.raises(installer.InstallError) as failure:
        installer.install_runtime("uv", "/opt/wayvia/wayvia-0.1.0-py3-none-any.whl")
    assert failure.value.code == "runtime_unreadable"


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


@pytest.mark.parametrize("after_write", [False, True])
def test_every_install_write_boundary_can_be_retried(tmp_path, inputs, monkeypatch, after_write):
    original = installer.write
    completed = []

    def count(path, data, mode):
        original(path, data, mode)
        completed.append(path)

    monkeypatch.setattr(installer, "write", count)
    prepare(tmp_path / "count", inputs)
    for failure_at in range(1, len(completed) + 1):
        stage = tmp_path / f"interrupted-{failure_at}"
        stage.mkdir()
        unrelated = stage / "operator-file"
        unrelated.write_text("keep this unowned file")
        calls = 0

        def interrupted(path, data, mode, failure_at=failure_at):
            nonlocal calls
            calls += 1
            if calls == failure_at and not after_write:
                raise InterruptedError("injected before atomic write")
            original(path, data, mode)
            if calls == failure_at and after_write:
                raise InterruptedError("injected after atomic write")

        monkeypatch.setattr(installer, "write", interrupted)
        with pytest.raises(InterruptedError):
            prepare(stage, inputs)
        monkeypatch.setattr(installer, "write", original)
        assert prepare(stage, inputs)["status"] == "prepared"
        assert not (stage / installer.JOURNAL).exists()
        assert unrelated.read_text() == "keep this unowned file"
        assert installer.uninstall(stage)["status"] == "uninstalled_data_retained"


def test_interrupted_first_install_can_be_uninstalled(tmp_path, inputs, monkeypatch):
    original = installer.write
    stage = tmp_path / "stage"

    def fail(path, data, mode):
        original(path, data, mode)
        if path.name == "sing-box":
            raise InterruptedError("injected after payload write")

    monkeypatch.setattr(installer, "write", fail)
    with pytest.raises(InterruptedError):
        prepare(stage, inputs)
    monkeypatch.setattr(installer, "write", original)
    result = installer.uninstall(stage)
    assert result["status"] == "uninstalled_interrupted_layout"
    assert not (stage / "usr/local/bin/sing-box").exists()
    assert not (stage / installer.JOURNAL).exists()


def test_interrupted_upgrade_restores_deleted_and_replaced_files(tmp_path, inputs, monkeypatch):
    source, _, _ = inputs
    obsolete = source / "src/wayvia/obsolete.py"
    obsolete.write_text("old module")
    stage = tmp_path / "stage"
    prepare(stage, inputs)
    previous = (stage / installer.MANIFEST).read_bytes()
    obsolete.unlink()
    new_module = source / "src/wayvia/new.py"
    new_module.write_text("new module")
    init = source / "src/wayvia/__init__.py"
    before = init.read_bytes()
    init.write_text('revision = "upgraded"\n')
    original = installer.write

    def fail_commit(path, data, mode):
        if path.name == "install-manifest.json":
            raise InterruptedError("injected before manifest commit")
        original(path, data, mode)

    monkeypatch.setattr(installer, "write", fail_commit)
    with pytest.raises(InterruptedError):
        prepare(stage, inputs)
    assert not (stage / "opt/wayvia/source/src/wayvia/obsolete.py").exists()
    monkeypatch.setattr(installer, "write", original)
    result = installer.recover_layout(stage)
    assert result["status"] == "interrupted_installation_rolled_back"
    assert (stage / "opt/wayvia/source/src/wayvia/obsolete.py").read_text() == "old module"
    assert (stage / "opt/wayvia/source/src/wayvia/__init__.py").read_bytes() == before
    assert not (stage / "opt/wayvia/source/src/wayvia/new.py").exists()
    assert (stage / installer.MANIFEST).read_bytes() == previous
    assert prepare(stage, inputs)["status"] == "prepared"


def test_recovery_refuses_post_failure_operator_changes(tmp_path, inputs, monkeypatch):
    stage = tmp_path / "stage"
    original = installer.write

    def fail(path, data, mode):
        original(path, data, mode)
        if path.name == "sing-box":
            raise InterruptedError("injected after payload write")

    monkeypatch.setattr(installer, "write", fail)
    with pytest.raises(InterruptedError):
        prepare(stage, inputs)
    monkeypatch.setattr(installer, "write", original)
    target = stage / "usr/local/bin/sing-box"
    target.write_text("operator replacement")
    with pytest.raises(installer.InstallError) as failure:
        installer.recover_layout(stage)
    assert failure.value.code == "recovery_file_modified"
    assert target.read_text() == "operator replacement"
    assert (stage / installer.JOURNAL).exists()


def test_upgrade_removes_obsolete_files_and_retains_modified_ones(tmp_path, inputs):
    source, _, wheel = inputs
    obsolete = source / "src/wayvia/obsolete.py"
    modified = source / "src/wayvia/modified.py"
    obsolete.write_text("old module")
    modified.write_text("original module")
    stage = tmp_path / "stage"
    prepare(stage, inputs)
    installed_modified = stage / "opt/wayvia/source/src/wayvia/modified.py"
    installed_modified.write_text("operator change")
    obsolete.unlink()
    modified.unlink()
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("wayvia/__init__.py", '__version__ = "0.2.0"')
        archive.writestr(
            "wayvia-0.2.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: wayvia\nVersion: 0.2.0\n",
        )
    result = prepare(stage, inputs)
    assert "opt/wayvia/source/src/wayvia/obsolete.py" in result["obsolete_files_removed"]
    assert "opt/wayvia/wayvia-0.1.0-py3-none-any.whl" in result["obsolete_files_removed"]
    assert not (stage / "opt/wayvia/wayvia-0.1.0-py3-none-any.whl").exists()
    assert (stage / "opt/wayvia/wayvia-0.2.0-py3-none-any.whl").is_file()
    assert result["modified_obsolete_files_retained"] == [
        "opt/wayvia/source/src/wayvia/modified.py"
    ]
    manifest = json.loads((stage / installer.MANIFEST).read_text())
    assert "opt/wayvia/source/src/wayvia/modified.py" in manifest["files"]
    removed = installer.uninstall(stage)
    assert removed["modified_files_retained"] == ["opt/wayvia/source/src/wayvia/modified.py"]
    assert installed_modified.read_text() == "operator change"
    assert not (stage / "opt/wayvia/source/src/wayvia/obsolete.py").exists()
