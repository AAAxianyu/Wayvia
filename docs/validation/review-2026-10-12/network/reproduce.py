"""Read-only network state-machine audit; no host networking or source writes.

Run from the repository: .venv/bin/python docs/validation/review-2026-10-12/network/reproduce.py
Uses the existing explicitly simulated test executor/timer with production manager.
The host-conflict case stubs read-only command observations. This is not kernel evidence.
"""
import json
import runpy
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from wayvia.adapters.base import CommandResult
from wayvia.adapters.linux import LinuxExecutor
from wayvia.agent.planner import build_plan
from wayvia.agent.models import Probe
from wayvia.agent.validation import validate_plan_topology, validate_server_artifact
from wayvia.config import WayviaConfig, example_config
from wayvia.render import ServerRenderer

test = runpy.run_path("tests/unit/test_transactions.py")
results = {"scope": "production TransactionManager with explicitly simulated test executor; read-only preflight stub"}

with tempfile.TemporaryDirectory(prefix="wayvia-audit-") as directory:
    manager = test["manager"](Path(directory).resolve())
    first = manager.confirm(manager.apply(test["plan"]())["id"])
    second = manager.confirm(manager.apply(test["plan"]())["id"])
    manager.rollback(second["id"])
    manager.executor.events.clear()
    result = manager.recover(rebuild_active=True)
    results["rollback_confirmed_successor"] = {
        "predecessor_state": manager._read(first["id"])["state"],
        "successor_state": manager._read(second["id"])["state"],
        "rebuild_result": result,
        "rebuild_actions": manager.executor.events,
    }

with tempfile.TemporaryDirectory(prefix="wayvia-audit-") as directory:
    manager = test["manager"](Path(directory).resolve())
    current = manager.confirm(manager.apply(test["plan"]())["id"])
    manager.verifier = lambda executor, probes: [{"status": "failed"}]
    initial_fault = manager.monitor(current["id"])["state"]
    manager.verifier = test["healthy"]
    healthy_again = manager.monitor(current["id"])["state"]
    recovery = manager.recover(rebuild_active=True)
    try:
        manager.apply(test["plan"]())
        error = None
    except Exception as exc:
        error = exc.code
    results["degraded_recovery_dead_end"] = {
        "initial_fault": initial_fault,
        "healthy_again": healthy_again,
        "explicit_recover": recovery,
        "fresh_apply_error": error,
    }

with tempfile.TemporaryDirectory(prefix="wayvia-audit-") as directory:
    manager = test["manager"](Path(directory).resolve())
    first = manager.confirm(manager.apply(test["plan"]())["id"])
    second = manager.apply(test["plan"]())
    manager.executor.events.clear()
    manager.verifier = lambda executor, probes: [{"status": "failed"}]
    manager.monitor(first["id"])
    results["monitor_old_active_during_pending_successor"] = {
        "old_state": manager._read(first["id"])["state"],
        "new_state": manager._read(second["id"])["state"],
        "events": manager.executor.events,
    }

class ObservationRunner:
    def __init__(self):
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        if argv == ["ip", "-j", "address", "show"]:
            return CommandResult(0, json.dumps([
                {"ifname": "eth0", "addr_info": [{"family": "inet", "local": "192.0.2.4", "prefixlen": 24}]},
                {"ifname": "tun0", "addr_info": [{"family": "inet", "local": "10.77.0.1", "prefixlen": 24}]},
            ]))
        if argv == ["ss", "-H", "-ltnp"]:
            return CommandResult(0, "")
        raise AssertionError(argv)

artifacts = ServerRenderer(WayviaConfig.model_validate(example_config())).render(
    "exit", [], {"carrier-credential": "c" * 32}
)
plan = build_plan("exit", "1", artifacts, "baseline", exit_interface="eth0")
runner = ObservationRunner()
executor = LinuxExecutor(runner=runner, enforce_environment=False)
executor.validate_host_conflicts(plan)
results["exit_unowned_tun0_and_subnet_overlap"] = {
    "preflight_result": "accepted",
    "commands_queried": runner.calls,
}

class RecordingExecutor(test["Executor"]):
    def apply(self, action):
        super().apply(action)
        if action.resource == "ingress.json":
            self.installed_users = json.loads(action.content)["inbounds"][0]["users"]

with tempfile.TemporaryDirectory(prefix="wayvia-audit-") as directory:
    executor = RecordingExecutor()
    manager = test["manager"](Path(directory).resolve(), executor=executor)
    rendered = ServerRenderer(WayviaConfig.model_validate(example_config())).render(
        "ingress", [{"id": "revoked-device", "password": "old-synthetic-password-at-least-24"}],
        {"carrier-credential": "c" * 32},
    )
    selected = [Probe.model_validate(row) for row in json.loads(Path("examples/ingress-probes.json").read_text())]
    stale = build_plan("ingress", "1", rendered, "baseline", probes=selected)
    validate_plan_topology(stale)
    for name, content in rendered.items():
        validate_server_artifact(name, content)
    manager.sync_devices([], 2)
    result = manager.apply(stale)
    results["apply_does_not_enforce_latest_device_checkpoint"] = {
        "real_render_and_topology_validation": "passed",
        "checkpoint_devices": manager._credentials()["devices"],
        "installed_device_names": [user["name"] for user in executor.installed_users],
        "transaction_state": result["state"],
        "note": "A real race can render old users, revoke, then capture the new baseline; normal apply does not overlay the checkpoint as recovery does.",
    }

class Clock(datetime):
    offset = 0

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + timedelta(seconds=cls.offset)

with tempfile.TemporaryDirectory(prefix="wayvia-audit-") as directory:
    manager = test["manager"](Path(directory).resolve())
    current = manager.apply(test["plan"]())

    def delayed_healthy(executor, probes):
        Clock.offset = 121
        return test["healthy"](executor, probes)

    manager.verifier = delayed_healthy
    with patch("wayvia.agent.transactions.datetime", Clock):
        result = manager.confirm(current["id"])
    record = manager._read(current["id"])
    results["confirmation_passes_deadline_during_probes"] = {
        "state": result["state"],
        "confirmed_after_deadline": datetime.fromisoformat(record["confirmed_at"]) > datetime.fromisoformat(record["deadline"]),
    }

print(json.dumps(results, indent=2))
