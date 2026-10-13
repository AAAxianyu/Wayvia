"""Explicit simulated-adapter proof of the documented two-state-directory hazard.

Uses real ControlService/TransactionManager/SQLite/credential files, an explicitly
simulated executor, and temporary directories. Never runs the privileged agent.
"""
import importlib.util
import json
import tempfile
from pathlib import Path

from wayvia.config import WayviaConfig, example_config
from wayvia.services.control import ControlService

spec = importlib.util.spec_from_file_location(
    "audit_transaction_fixture", Path("tests/unit/test_transactions.py")
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

with tempfile.TemporaryDirectory(prefix="wayvia-state-directory-audit-") as temporary:
    root = Path(temporary).resolve()
    manager = fixture.manager(root / "agent")

    class ExplicitTestDataplane:
        simulated = True
        block_calls = 0
        last_error = None

        def sync(self, devices, revision):
            try:
                result = manager.sync_devices(devices, revision)
            except Exception as error:
                self.last_error = error.code
                raise
            return {**result, "status": "active", "simulated": True}

        def block(self):
            self.block_calls += 1
            return {"status": "blocked", "simulated": True}

    dataplane = ExplicitTestDataplane()
    config = WayviaConfig.model_validate(example_config())
    cli = ControlService(root / "dot-wayvia", config, mode="test", dataplane=dataplane)
    api = ControlService(root / "installed-control", config, mode="test", dataplane=dataplane)
    first = cli.add("cli-phone", "android")
    second = api.add("api-phone", "ios")
    print(json.dumps({
        "case": "cli_and_system_api_use_different_state_directories",
        "mode": "explicit_test_simulated_network_executor",
        "cli_device_status": first["status"],
        "api_device_status": second["status"],
        "rejected_revision_code": dataplane.last_error,
        "ingress_block_requests": dataplane.block_calls,
        "cli_list_count": len(cli.devices()),
        "api_list_count": len(api.devices()),
    }))
