"""Nonprivileged installer audit; all generated files are temporary test fixtures.

Run from the Wayvia repository root with .venv/bin/python. No real host layout,
service, network, credentials or unrelated workspace files are accessed.
"""
import importlib.util
import json
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "audit_installer_fixture", Path("tests/integration/test_install.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory(prefix="wayvia-installer-audit-") as temporary:
    root = Path(temporary).resolve()  # macOS /var is a symlink to /private/var.
    inputs = module.inputs.__wrapped__(root)
    source, cache, wheel = inputs

    obsolete = source / "src/wayvia/obsolete.py"
    obsolete.write_text("old implementation\n")
    stage = root / "upgrade-stage"
    module.prepare(stage, inputs)
    obsolete.unlink()
    module.prepare(stage, inputs)
    target = stage / "opt/wayvia/source/src/wayvia/obsolete.py"
    manifest = json.loads((stage / module.installer.MANIFEST).read_text())
    after_upgrade = {
        "obsolete_file_exists": target.exists(),
        "obsolete_file_owned": str(target.relative_to(stage)) in manifest["files"],
    }
    module.installer.uninstall(stage)
    print(json.dumps({
        "case": "removed_source_after_upgrade_then_uninstall",
        **after_upgrade,
        "obsolete_after_uninstall": target.exists(),
    }))

    broken = root / "interrupted-stage"
    original = module.installer.write
    calls = 0

    def fail_second(path, data, mode):
        global calls
        calls += 1
        if calls == 2:
            raise OSError("injected disk write failure")
        return original(path, data, mode)

    module.installer.write = fail_second
    try:
        module.prepare(broken, inputs)
    except OSError:
        pass
    finally:
        module.installer.write = original
    retry_code = None
    try:
        module.prepare(broken, inputs)
    except module.installer.InstallError as error:
        retry_code = error.code
    uninstall_code = None
    try:
        module.installer.uninstall(broken)
    except module.installer.InstallError as error:
        uninstall_code = error.code
    print(json.dumps({
        "case": "first_install_write_interruption",
        "files_written": sum(path.is_file() for path in broken.rglob("*")),
        "manifest_exists": (broken / module.installer.MANIFEST).exists(),
        "retry_error": retry_code,
        "uninstall_error": uninstall_code,
    }))
