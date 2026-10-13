"""Credential, running-generation and recovery regressions with real rendered plans.

The executor models owned resources in memory; it never operates the host network.
Plan topology/artifact validation and the transaction journal are production code.
"""

import base64
import errno
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_transactions import Executor, Timer

from wayvia.adapters.base import AdapterError
from wayvia.adapters.linux import LinuxExecutor
from wayvia.agent.models import Probe, State
from wayvia.agent.planner import build_plan
from wayvia.agent.transactions import TransactionManager
from wayvia.agent.validation import validate_plan_topology, validate_server_artifact
from wayvia.config import WayviaConfig, example_config
from wayvia.render import ServerRenderer


class Resources(Executor):
    def __init__(self):
        super().__init__()
        self.resources = {}
        self.available = True
        self.sync_failure = False
        self.config_dir = Path("/wayvia-test-uninstalled")

    @staticmethod
    def key(action):
        return action.kind, action.resource, action.namespace

    def preflight(self, plan):
        validate_plan_topology(plan)
        for action in plan.actions:
            if action.kind == "install_config":
                validate_server_artifact(action.resource, action.content)

    def capture(self, action):
        value = self.resources.get(self.key(action))
        result = {"exists": value is not None, "owned": value is not None, "value": value}
        if action.kind == "install_config" and value is not None:
            result.update(content=base64.b64encode(value.encode()).decode(), mode=0o600)
        if action.kind == "service":
            result["active"] = bool(value)
        return result

    def apply(self, action):
        super().apply(action)
        self.resources[self.key(action)] = (
            action.content if action.kind == "install_config" else True
        )

    def rollback(self, action, before):
        super().rollback(action, before)
        if before["exists"]:
            value = (
                base64.b64decode(before["content"]).decode()
                if action.kind == "install_config"
                else before["value"]
            )
            self.resources[self.key(action)] = value
        else:
            self.resources.pop(self.key(action), None)

    def protect(self, plan):
        super().protect(plan)
        unit = "wayvia-ingress.service" if plan.role == "ingress" else "wayvia-exit.service"
        self.resources[("service", unit, None)] = False

    def sync_devices(self, devices, revision):
        key = ("install_config", "ingress.json", None)
        if self.sync_failure:
            self.resources[("service", "wayvia-ingress.service", None)] = False
            raise AdapterError("DEVICE_SYNC_TEST_FAILURE", "Explicit failed test reconciliation")
        if key not in self.resources:
            raise FileNotFoundError(
                errno.ENOENT,
                "No initial deployment installed",
                str(self.config_dir / "ingress.json"),
            )
        config = json.loads(self.resources[key])
        config["inbounds"][0]["users"] = [
            {"name": device["id"], "password": device["password"]} for device in devices
        ]
        self.resources[key] = json.dumps(config)
        self.resources[("service", "wayvia-ingress.service", None)] = True
        return super().sync_devices(devices, revision)

    def validate_devices(self, devices):
        LinuxExecutor.validate_devices(self, devices)

    def _cmd(self, argv):
        super()._cmd(argv)
        if argv == ["systemctl", "stop", "wayvia-ingress.service"]:
            self.resources[("service", "wayvia-ingress.service", None)] = False

    def users(self):
        return json.loads(self.resources[("install_config", "ingress.json", None)])["inbounds"][0][
            "users"
        ]


def rendered_plan(port=443, users=None):
    data = example_config()
    data["entry"]["endpoint"] = f"entry.wayvia.example:{port}"
    config = WayviaConfig.model_validate(data)
    artifacts = ServerRenderer(config).render(
        "ingress",
        users if users is not None else [{"id": "device-one", "password": "d" * 32}],
        {"carrier-credential": "c" * 32},
    )
    probes = [
        Probe.model_validate(item)
        for item in json.loads(Path("examples/ingress-probes.json").read_text())
    ]
    probes = [
        probe.model_copy(update={"port": port}) if probe.layer == "access" else probe
        for probe in probes
    ]
    plan = build_plan("ingress", config.revision, artifacts, "baseline", probes=probes)
    validate_plan_topology(plan)
    for name, content in artifacts.items():
        validate_server_artifact(name, content)
    return plan


def observe_resources(executor, probes):
    config = json.loads(executor.resources[("install_config", "ingress.json", None)])
    listener = executor.resources.get(("service", "wayvia-ingress.service", None))
    return [
        {
            "layer": probe.layer,
            "status": "healthy"
            if executor.available
            and listener
            and (probe.layer != "access" or config["inbounds"][0]["listen_port"] == probe.port)
            else "failed",
        }
        for probe in probes
    ]


def manager(directory, executor=None, verifier=observe_resources):
    return TransactionManager(
        executor or Resources(), directory, Timer(), mode="test", verifier=verifier
    )


def deploy(manager, plan):
    pending = manager.apply(plan)
    assert pending["state"] == State.AWAITING_CONFIRM
    return manager.confirm(pending["id"])


def test_old_render_new_baseline_cannot_revive_revoked_password(tmp_path):
    manager_ = manager(tmp_path)
    old = rendered_plan(users=[{"id": "revoked-device", "password": "r" * 32}])
    # Device creation/revocation can precede the first network install. Desired
    # credentials remain durable even though there is no core configuration yet.
    with pytest.raises(AdapterError) as error:
        manager_.sync_devices([], 2)
    assert error.value.code == "INGRESS_NOT_DEPLOYED"
    result = manager_.apply(old)
    assert result["state"] == State.AWAITING_CONFIRM
    assert manager_.executor.users() == []
    assert result["device_revision"] == 2
    assert result["submitted_digest"] == old.digest
    assert result["effective_digest"] != old.digest
    journal = manager_._read(result["id"])
    installed = next(
        step for step in journal["steps"] if step["action"]["resource"] == "ingress.json"
    )
    assert json.loads(installed["action"]["content"])["inbounds"][0]["users"] == []


def test_three_confirmed_generations_rollback_reboot_and_uninstall(tmp_path):
    manager_ = manager(tmp_path)
    first = deploy(manager_, rendered_plan(443))
    second = deploy(manager_, rendered_plan(8443))
    third = deploy(manager_, rendered_plan(9443))
    manager_.sync_devices([], 4)
    assert manager_._read(third["id"])["predecessor_id"] == second["id"]
    assert manager_.rollback(third["id"])["state"] == State.ROLLED_BACK
    restored = next(row for row in manager_.status() if row["current"])
    assert restored["id"] == second["id"] and restored["state"] == State.ACTIVE
    assert restored["device_revision"] == 4 and manager_.executor.users() == []
    restarted = manager(tmp_path, manager_.executor)
    assert restarted.recover(rebuild_active=True)[0]["id"] == second["id"]
    assert restarted.rollback(second["id"])["state"] == State.ROLLED_BACK
    assert next(row for row in restarted.status() if row["current"])["id"] == first["id"]
    assert restarted.executor.users() == []
    assert restarted.rollback(first["id"])["state"] == State.ROLLED_BACK
    assert restarted.executor.resources == {}
    assert manager(tmp_path, restarted.executor).recover(rebuild_active=True) == []


def test_rollback_crash_after_predecessor_pointer_restoration_is_repeatable(tmp_path, monkeypatch):
    manager_ = manager(tmp_path)
    first = deploy(manager_, rendered_plan())
    second = deploy(manager_, rendered_plan(8443))
    save = manager_._save

    def interrupted_save(tx):
        if tx["id"] == second["id"] and tx["state"] == State.ROLLED_BACK:
            raise RuntimeError("simulated process interruption before final journal fsync")
        save(tx)

    monkeypatch.setattr(manager_, "_save", interrupted_save)
    with pytest.raises(RuntimeError):
        manager_.rollback(second["id"])
    restarted = manager(tmp_path, manager_.executor)
    assert restarted._read(second["id"])["state"] == State.ROLLING_BACK
    restarted.recover(rebuild_active=True)
    current = next(row for row in restarted.status() if row["current"])
    assert current["id"] == first["id"] and current["state"] == State.ACTIVE
    assert restarted._read(second["id"])["state"] == State.ROLLED_BACK


def test_old_port_monitor_cannot_stop_pending_successor(tmp_path):
    manager_ = manager(tmp_path)
    old = deploy(manager_, rendered_plan())
    new = manager_.apply(rendered_plan(8443))
    manager_.executor.events.clear()
    assert manager_.monitor(old["id"])["current"] is False
    assert manager_.executor.events == []
    assert manager_._read(old["id"])["state"] == State.ACTIVE
    assert manager_.confirm(new["id"])["state"] == State.ACTIVE
    assert manager_.monitor(old["id"])["state"] == State.SUPERSEDED
    current = manager_.monitor(new["id"])
    assert current["state"] == State.ACTIVE
    assert current["monitored_at"] and current["monitor_valid_until"]
    assert {item["generation"] for item in current["evidence"]} == {new["id"]}
    assert {item["revision"] for item in current["evidence"]} == {current["revision"]}


def test_runtime_failure_recovers_with_guard_retained_and_latest_credentials(tmp_path):
    manager_ = manager(tmp_path)
    current = deploy(manager_, rendered_plan())
    manager_.sync_devices([], 2)
    manager_.executor.available = False
    assert manager_.monitor(current["id"])["state"] == State.DEGRADED
    assert manager_.executor.resources[("guard", "wayvia_guard", "wayvia-egress")]
    manager_.executor.available = True
    manager_.executor.events.clear()
    recovered = manager_.monitor(current["id"])
    assert recovered["state"] == State.ACTIVE
    assert manager_.executor.users() == []
    assert manager_.executor.events[0] == ("protect", "ingress")
    assert not any(event == "rollback" for event, _ in manager_.executor.events)
    assert manager_.executor.resources[("guard", "wayvia_guard", "wayvia-egress")]


def test_runtime_degraded_can_be_replaced_by_a_new_plan(tmp_path):
    manager_ = manager(tmp_path)
    current = deploy(manager_, rendered_plan())
    manager_.executor.available = False
    manager_.monitor(current["id"])
    manager_.executor.available = True
    replacement = deploy(manager_, rendered_plan(8443))
    assert replacement["current"] and replacement["state"] == State.ACTIVE
    assert manager_._read(current["id"])["state"] == State.SUPERSEDED


@pytest.mark.parametrize("reason", ["explicit", "credential_failure"])
def test_safety_block_never_automatically_unblocks(tmp_path, reason):
    manager_ = manager(tmp_path)
    current = deploy(manager_, rendered_plan())
    if reason == "explicit":
        manager_.block_ingress()
    else:
        manager_.executor.sync_failure = True
        with pytest.raises(AdapterError):
            manager_.sync_devices([], 2)
        manager_.executor.sync_failure = False
    manager_.executor.events.clear()
    assert manager_.monitor(current["id"])["state"] == State.DEGRADED
    assert manager(tmp_path, manager_.executor).recover(rebuild_active=True) == []
    assert not any(event == "apply" for event, _ in manager_.executor.events)
    with pytest.raises(AdapterError) as error:
        manager_.apply(rendered_plan(8443))
    assert error.value.code == "INGRESS_BLOCKED"
    # Explicit, successful reconciliation is the deliberate recovery operation.
    manager_.sync_devices([], 2)
    assert manager_.monitor(current["id"])["state"] == State.ACTIVE
    assert manager_.executor.users() == []


def test_block_during_pending_change_survives_rollback_to_predecessor(tmp_path):
    manager_ = manager(tmp_path)
    old = deploy(manager_, rendered_plan())
    new = manager_.apply(rendered_plan(8443))
    manager_.block_ingress()
    with pytest.raises(AdapterError) as error:
        manager_.confirm(new["id"])
    assert error.value.code == "INGRESS_BLOCKED"
    manager_.rollback(new["id"])
    current = next(row for row in manager_.status() if row["current"])
    assert current["id"] == old["id"]
    assert current["state"] == State.DEGRADED
    assert current["blocked_reason"] == "EXPLICIT_INGRESS_BLOCK"
    assert manager_.monitor(old["id"])["state"] == State.DEGRADED


def test_reboot_retries_transient_handshake_before_active(tmp_path):
    manager_ = manager(tmp_path)
    current = deploy(manager_, rendered_plan())
    calls = []

    def connecting(executor, probes):
        calls.append(True)
        if len(calls) == 1:
            return [{"layer": probe.layer, "status": "failed"} for probe in probes]
        return observe_resources(executor, probes)

    restarted = manager(tmp_path, manager_.executor, verifier=connecting)
    restarted.verify_seconds = 0.3
    assert restarted.recover(rebuild_active=True)[0]["state"] == State.ACTIVE
    assert len(calls) == 2
    assert "error" not in restarted._read(current["id"])


def test_confirm_expiration_during_successful_probes_still_rolls_back(tmp_path, monkeypatch):
    manager_ = manager(tmp_path)
    pending = manager_.apply(rendered_plan())

    class Clock(datetime):
        offset = 0

        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=cls.offset)

    def slow_success(executor, probes):
        Clock.offset = 121
        return observe_resources(executor, probes)

    manager_.verifier = slow_success
    monkeypatch.setattr("wayvia.agent.transactions.datetime", Clock)
    with pytest.raises(AdapterError) as error:
        manager_.confirm(pending["id"])
    assert error.value.code == "CONFIRM_EXPIRED"
    assert manager_._read(pending["id"])["state"] == State.ROLLED_BACK
    assert manager_.executor.resources == {}


def test_confirm_supplies_remaining_budget_to_production_verifier(tmp_path, monkeypatch):
    manager_ = manager(tmp_path)
    pending = manager_.apply(rendered_plan())
    record = manager_._read(pending["id"])
    record["deadline"] = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
    manager_._save(record)
    budgets = []

    def production_signature(executor, probes, *, deadline_monotonic):
        import time

        budgets.append(deadline_monotonic - time.monotonic())
        return observe_resources(executor, probes)

    monkeypatch.setattr("wayvia.agent.transactions.verify", production_signature)
    manager_.verifier = production_signature
    assert manager_.confirm(pending["id"])["state"] == State.ACTIVE
    assert 0 < budgets[0] <= 2


def test_failed_monitor_stop_never_leaves_a_healthy_current_record(tmp_path, monkeypatch):
    manager_ = manager(tmp_path)
    current = deploy(manager_, rendered_plan())
    manager_.executor.available = False

    def cannot_stop(plan):
        raise AdapterError("COMMAND_FAILED", "Simulated systemd stop failure")

    monkeypatch.setattr(manager_.executor, "protect", cannot_stop)
    result = manager_.monitor(current["id"])
    assert result["state"] == State.DEGRADED
    assert manager_._read(current["id"])["protection"] == "unconfirmed"
    assert all(item["status"] == "failed" for item in result["evidence"])


def test_boot_enforces_block_persisted_before_a_crashed_stop_command(tmp_path):
    manager_ = manager(tmp_path)
    current = deploy(manager_, rendered_plan())
    manager_._set_block("ingress", "EXPLICIT_INGRESS_BLOCK")
    assert manager_.executor.resources[("service", "wayvia-ingress.service", None)]
    restarted = manager(tmp_path, manager_.executor)
    restarted.recover(rebuild_active=True)
    assert not manager_.executor.resources[("service", "wayvia-ingress.service", None)]
    assert restarted._read(current["id"])["state"] == State.DEGRADED


def test_stale_rollback_timer_cannot_restore_a_confirmed_or_superseded_generation(tmp_path):
    manager_ = manager(tmp_path)
    first = deploy(manager_, rendered_plan())
    second = deploy(manager_, rendered_plan(8443))
    manager_.executor.events.clear()
    assert manager_.rollback(first["id"], only_unconfirmed=True)["state"] == State.SUPERSEDED
    assert manager_.rollback(second["id"], only_unconfirmed=True)["state"] == State.ACTIVE
    manager_.executor.available = False
    manager_.monitor(second["id"])
    manager_.executor.events.clear()
    assert manager_.rollback(second["id"], only_unconfirmed=True)["state"] == State.DEGRADED
    assert manager_.executor.events == []


def test_only_public_transaction_metadata_contains_no_plan_credentials(tmp_path):
    manager_ = manager(tmp_path)
    current = deploy(manager_, rendered_plan())
    public = json.dumps(manager_.status())
    assert "d" * 32 not in public and "c" * 32 not in public
    assert current["verification_scope"] == "datapath"
    assert {item["scope"] for item in current["evidence"]} == {"datapath"}


def test_valid_initial_reconciliation_clears_block_without_claiming_active(tmp_path):
    manager_ = manager(tmp_path)
    manager_.block_ingress()
    devices = [{"id": "reconciled-device", "password": "n" * 32}]
    with pytest.raises(AdapterError) as error:
        manager_.sync_devices(devices, 2)
    assert error.value.code == "INGRESS_NOT_DEPLOYED"
    assert manager_._runtime("ingress")["blocked_reason"] is None
    assert manager_.status() == []
    current = deploy(manager_, rendered_plan())
    assert current["device_revision"] == 2
    assert manager_.executor.users() == [{"name": "reconciled-device", "password": "n" * 32}]


def test_repaired_initial_secret_reconciles_then_bootstraps_real_control_state(tmp_path):
    from wayvia.services.control import ControlService

    manager_ = manager(tmp_path / "agent")

    class TestDataplane:
        simulated = True

        def sync(self, devices, revision):
            result = manager_.sync_devices(devices, revision)
            return {**result, "status": "active", "simulated": True}

        def block(self):
            return manager_.block_ingress()

    service = ControlService(
        tmp_path / "control",
        WayviaConfig.model_validate(example_config()),
        mode="test",
        dataplane=TestDataplane(),
    )
    device = service.add("pending-phone", "android")
    assert device["status"] == "pending"
    with service.db.connect() as connection:
        ref = connection.execute(
            "SELECT secret_ref FROM devices WHERE id=?", (device["id"],)
        ).fetchone()[0]
    path = service.secrets.root / ref
    path.chmod(0o644)  # A real SecretStore read now fails its permission check.
    assert service.reconcile()["code"] == "dataplane_apply_failed"
    assert manager_._runtime("ingress")["blocked_reason"] == "EXPLICIT_INGRESS_BLOCK"
    path.chmod(0o600)
    assert service.reconcile()["code"] == "INGRESS_NOT_DEPLOYED"
    assert service.device(device["id"])["status"] == "pending"
    assert manager_._runtime("ingress")["blocked_reason"] is None
    deploy(manager_, rendered_plan())
    assert service.reconcile()["status"] == "active"
    assert service.device(device["id"])["status"] == "active"
    assert manager_.executor.users() == [
        {"name": device["id"], "password": service.secrets.read(ref)}
    ]


@pytest.mark.parametrize(
    ("devices", "revision", "code"),
    [
        ([{"id": "device", "password": "short"}], 4, "DEVICE_INPUT_INVALID"),
        ([], -1, "REVISION_INVALID"),
        ([], 2, "REVISION_STALE"),
    ],
)
def test_invalid_initial_checkpoint_cannot_clear_protection(tmp_path, devices, revision, code):
    manager_ = manager(tmp_path)
    manager_.block_ingress()
    (tmp_path / "device-state.json").write_text(json.dumps({"devices": [], "revision": 3}))
    with pytest.raises(AdapterError) as error:
        manager_.sync_devices(devices, revision)
    assert error.value.code == code
    assert manager_._runtime("ingress")["blocked_reason"] == "EXPLICIT_INGRESS_BLOCK"
    assert manager_._credentials()["revision"] == 3


@pytest.mark.parametrize("filename", [None, "/wayvia-test-uninstalled/other.json"])
def test_other_missing_file_cannot_clear_initial_block(tmp_path, monkeypatch, filename):
    manager_ = manager(tmp_path)
    manager_.block_ingress()

    def missing(devices, revision):
        raise FileNotFoundError(errno.ENOENT, "Other missing input", filename)

    monkeypatch.setattr(manager_.executor, "sync_devices", missing)
    with pytest.raises(FileNotFoundError):
        manager_.sync_devices([], 2)
    assert manager_._runtime("ingress")["blocked_reason"] == "EXPLICIT_INGRESS_BLOCK"


def test_initial_checkpoint_write_failure_cannot_clear_block(tmp_path, monkeypatch):
    import wayvia.agent.transactions as module

    manager_ = manager(tmp_path)
    manager_.block_ingress()
    original = module.atomic_write

    def fail_checkpoint(path, *args, **kwargs):
        if Path(path).name == "device-state.json":
            raise OSError("isolated checkpoint persistence failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(module, "atomic_write", fail_checkpoint)
    with pytest.raises(OSError):
        manager_.sync_devices([], 2)
    assert manager_._runtime("ingress")["blocked_reason"] == "EXPLICIT_INGRESS_BLOCK"
