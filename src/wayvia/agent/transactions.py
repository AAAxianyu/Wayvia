"""Fsync journals, role locks, independent rollback, and reboot recovery."""

import fcntl
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wayvia.adapters.base import AdapterError, require_real
from wayvia.adapters.linux import atomic_write
from wayvia.agent.models import LAYERS, Action, Plan, State
from wayvia.agent.probes import verify


class SystemdTimer:
    simulated = False

    def __init__(
        self,
        runner,
        python="/opt/wayvia/.venv/bin/python",
        config_dir="/etc/wayvia",
        unit_dir="/etc/systemd/system",
    ):
        self.runner, self.python = runner, python
        self.config_dir, self.unit_dir = str(config_dir), str(unit_dir)

    def arm(self, transaction_id, seconds, journal_dir):
        self.runner.run(
            [
                "systemd-run",
                "--unit=wayvia-rollback-" + transaction_id,
                f"--on-active={seconds}s",
                "--timer-property=AccuracySec=1s",
                "--property=Type=oneshot",
                self.python,
                "-m",
                "wayvia.agent.recovery",
                "--journal-dir",
                str(journal_dir),
                "--config-dir",
                self.config_dir,
                "--unit-dir",
                self.unit_dir,
                "--transaction",
                transaction_id,
            ]
        )

    def cancel(self, transaction_id):
        self.runner.run(["systemctl", "stop", "wayvia-rollback-" + transaction_id + ".timer"])


class TransactionManager:
    def __init__(self, executor, journal_dir, timer=None, *, mode="production", verifier=verify):
        require_real(executor, mode)
        self.executor = executor
        self.root = Path(journal_dir)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(path.is_symlink() for path in (self.root, *self.root.parents)):
            raise AdapterError("UNSAFE_PATH", "Journal cannot be a symlink")
        if mode == "production" and self.root.stat().st_uid != os.geteuid():
            raise AdapterError(
                "JOURNAL_OWNER", "Journal directory must belong to the privileged agent"
            )
        self.root.chmod(0o700)
        self.timer = timer or SystemdTimer(
            executor.runner, config_dir=executor.config_dir, unit_dir=executor.unit_dir
        )
        require_real(self.timer, mode)
        self.verifier = verifier
        self.verify_seconds = 20 if mode == "production" else 0

    @contextmanager
    def lock(self, role):
        with (self.root / f"{role}.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def _path(self, transaction_id):
        if not re.fullmatch(r"[a-f0-9]{32}", transaction_id):
            raise AdapterError("TRANSACTION_INVALID", "Transaction identifier is invalid")
        return self.root / (transaction_id + ".json")

    def _read(self, transaction_id):
        path = self._path(transaction_id)
        if not path.exists():
            raise AdapterError("TRANSACTION_MISSING", "Transaction does not exist")
        return json.loads(path.read_text())

    def _save(self, tx):
        tx["updated_at"] = datetime.now(UTC).isoformat()
        atomic_write(self._path(tx["id"]), json.dumps(tx, sort_keys=True))

    def public(self, tx):
        return {
            k: tx.get(k)
            for k in (
                "id",
                "role",
                "revision",
                "state",
                "deadline",
                "updated_at",
                "evidence",
                "error",
            )
        }

    def status(self):
        return [
            self.public(json.loads(path.read_text()))
            for path in sorted(self.root.glob("*.json"))
            if re.fullmatch(r"[a-f0-9]{32}\.json", path.name)
        ]

    def apply(self, plan):
        if not isinstance(plan, Plan):
            plan = Plan.model_validate(plan)
        with self.lock(plan.role):
            if any(
                t["role"] == plan.role
                and t["state"] not in {State.ACTIVE, State.SUPERSEDED, State.ROLLED_BACK}
                for t in self.status()
            ):
                raise AdapterError(
                    "TRANSACTION_PENDING",
                    "Role already has an unconfirmed or unrecovered transaction",
                )
            plan.check(self.executor.baseline(plan.actions))
            self.executor.preflight(plan)
            tx = {
                "id": uuid.uuid4().hex,
                "role": plan.role,
                "revision": plan.revision,
                "state": State.APPLYING,
                "plan": plan.model_dump(mode="json"),
                "steps": [],
                "deadline": (
                    datetime.now(UTC) + timedelta(seconds=plan.confirm_seconds)
                ).isoformat(),
            }
            self._save(tx)
            try:
                # Timer installation succeeds before any mutating action starts.
                self.timer.arm(tx["id"], plan.confirm_seconds, self.root)
                for action in plan.actions:
                    before = self.executor.capture(action)
                    tx["steps"].append(
                        {
                            "action": action.model_dump(mode="json"),
                            "before": before,
                            "restored": False,
                        }
                    )
                    self._save(tx)
                    self.executor.apply(action)
                tx["state"] = State.VERIFYING
                self._save(tx)
                until = min(
                    time.monotonic() + self.verify_seconds,
                    time.monotonic()
                    + max(
                        0,
                        (
                            datetime.fromisoformat(tx["deadline"]) - datetime.now(UTC)
                        ).total_seconds(),
                    ),
                )
                while True:
                    tx["evidence"] = self.verifier(self.executor, plan.probes)
                    if self._healthy(tx["evidence"], plan) or time.monotonic() >= until:
                        break
                    time.sleep(0.2)
                if not self._healthy(tx["evidence"], plan):
                    raise AdapterError("PROBE_FAILED", "One or more required path probes failed")
                if datetime.now(UTC) >= datetime.fromisoformat(tx["deadline"]):
                    raise AdapterError("APPLY_DEADLINE", "Apply exceeded its confirmation window")
                tx["state"] = State.AWAITING_CONFIRM
                self._save(tx)
            except Exception as exc:
                tx["error"] = getattr(exc, "code", "APPLY_FAILED")
                self._rollback(tx)
            return self.public(tx)

    def _healthy(self, evidence, plan):
        return (
            len(evidence) == len(plan.probes) == len(LAYERS)
            and {item.get("layer") for item in evidence} == LAYERS
            and all(item.get("status") == "healthy" for item in evidence)
        )

    def confirm(self, transaction_id):
        tx = self._read(transaction_id)
        with self.lock(tx["role"]):
            tx = self._read(transaction_id)
            if tx["state"] != State.AWAITING_CONFIRM:
                raise AdapterError(
                    "CONFIRM_STATE_INVALID",
                    "Only a successfully verified transaction can be confirmed",
                )
            if datetime.now(UTC) >= datetime.fromisoformat(tx["deadline"]):
                self._rollback(tx)
                raise AdapterError(
                    "CONFIRM_EXPIRED", "Confirmation window expired; rollback was initiated"
                )
            plan = Plan.model_validate(tx["plan"])
            tx["evidence"] = self.verifier(self.executor, plan.probes)
            if not self._healthy(tx["evidence"], plan):
                self._rollback(tx)
                raise AdapterError(
                    "PROBE_FAILED", "Current probes failed; transaction was rolled back"
                )
            tx["state"] = State.ACTIVE
            tx["confirmed_at"] = datetime.now(UTC).isoformat()
            self._save(tx)  # Recovery process checks state, so a racing timer is harmless.
            for info in self.status():
                if (
                    info["id"] != tx["id"]
                    and info["role"] == tx["role"]
                    and info["state"] == State.ACTIVE
                ):
                    previous = self._read(info["id"])
                    previous["state"] = State.SUPERSEDED
                    self._save(previous)
            try:
                self.timer.cancel(tx["id"])
            except AdapterError:
                tx["timer_cleanup"] = "retry_on_recovery"
                self._save(tx)
            return self.public(tx)

    def _rollback(self, tx):
        tx["state"] = State.ROLLING_BACK
        self._save(tx)
        failures = []
        for step in reversed(tx["steps"]):
            if step.get("restored"):
                continue
            try:
                action = Action.model_validate(step["action"])
                before = self._credential_snapshot(action, step["before"])
                self.executor.rollback(action, before)
                step["restored"] = True
                self._save(tx)
            except Exception as exc:
                failures.append(getattr(exc, "code", "RESTORE_FAILED"))
        if not failures:
            try:
                steps = [
                    {"action": Action.model_validate(s["action"]), "before": s["before"]}
                    for s in tx["steps"]
                ]
                self.executor.restore_services(steps)
            except Exception as exc:
                failures.append(getattr(exc, "code", "RESTORE_FAILED"))
        tx["state"] = State.RECOVERY_REQUIRED if failures else State.ROLLED_BACK
        if failures:
            tx["error"] = "RECOVERY_REQUIRED"
            try:
                self.executor.protect(Plan.model_validate(tx["plan"]))
            except Exception:
                tx["protection"] = "unconfirmed"
        self._save(tx)
        try:
            self.timer.cancel(tx["id"])
        except AdapterError:
            pass

    def rollback(self, transaction_id, *, only_unconfirmed=False):
        tx = self._read(transaction_id)
        with self.lock(tx["role"]):
            tx = self._read(transaction_id)
            if tx["state"] == State.SUPERSEDED:
                raise AdapterError(
                    "TRANSACTION_SUPERSEDED",
                    "A newer configuration depends on this transaction; "
                    "create a fresh rollback plan",
                )
            if tx["state"] == State.ROLLED_BACK or (
                only_unconfirmed and tx["state"] == State.ACTIVE
            ):
                return self.public(tx)
            self._rollback(tx)
            return self.public(tx)

    def recover(self, *, rebuild_active=False):
        # A crash between saving the new ACTIVE state and superseding its
        # predecessor must never replay both confirmed configurations at boot.
        for role in ("ingress", "exit"):
            with self.lock(role):
                active = [
                    self._read(t["id"])
                    for t in self.status()
                    if t["role"] == role and t["state"] == State.ACTIVE
                ]
                if len(active) > 1:
                    newest = max(
                        active, key=lambda t: t.get("confirmed_at", t["plan"]["created_at"])
                    )
                    for tx in active:
                        if tx["id"] != newest["id"]:
                            tx["state"] = State.SUPERSEDED
                            self._save(tx)
        results = []
        for info in self.status():
            if info["state"] not in {
                State.ACTIVE,
                State.SUPERSEDED,
                State.ROLLED_BACK,
                State.DEGRADED,
            }:
                results.append(self.rollback(info["id"], only_unconfirmed=True))
            elif info["state"] == State.ACTIVE and rebuild_active:
                with self.lock(info["role"]):
                    tx = self._read(info["id"])
                    plan = Plan.model_validate(tx["plan"])
                    try:
                        self.executor.protect(plan)
                        # Establish guard before any accepted business listener.
                        actions = sorted(
                            plan.actions,
                            key=lambda a: {
                                "namespace": 0,
                                "guard": 1,
                                "install_config": 2,
                                "veth": 3,
                                "exit_nat": 4,
                                "service": 5,
                            }[a.kind],
                        )
                        for action in actions:
                            action = self._credential_action(action)
                            self.executor.apply(action)
                        tx["evidence"] = self.verifier(self.executor, plan.probes)
                        if not self._healthy(tx["evidence"], plan):
                            raise AdapterError("PROBE_FAILED", "Recovery probes failed")
                    except Exception as exc:
                        tx["state"] = State.DEGRADED
                        tx["error"] = getattr(exc, "code", "RECOVERY_FAILED")
                        self.executor.protect(plan)
                    self._save(tx)
                    results.append(self.public(tx))
        return results

    def _credentials(self):
        path = self.root / "device-state.json"
        return json.loads(path.read_text()) if path.exists() else None

    def _credential_action(self, action):
        current = self._credentials()
        if current and action.kind == "install_config" and action.resource == "ingress.json":
            config = json.loads(action.content)
            for inbound in config["inbounds"]:
                if inbound["type"] == "trojan":
                    inbound["users"] = [
                        {"name": d["id"], "password": d["password"]} for d in current["devices"]
                    ]
            return action.model_copy(update={"content": json.dumps(config)})
        return action

    def _credential_snapshot(self, action, before):
        if (
            action.kind == "install_config"
            and action.resource == "ingress.json"
            and before.get("exists")
        ):
            import base64

            previous = action.model_copy(
                update={"content": base64.b64decode(before["content"]).decode()}
            )
            adjusted = self._credential_action(previous)
            return {**before, "content": base64.b64encode(adjusted.content.encode()).decode()}
        return before

    def sync_devices(self, devices, revision):
        with self.lock("ingress"):
            self.executor.validate_devices(devices)
            current = self._credentials()
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
                raise AdapterError(
                    "REVISION_INVALID", "Device revision must be a nonnegative integer"
                )
            if current and revision <= current["revision"]:
                if revision == current["revision"] and devices == current["devices"]:
                    return self.executor.sync_devices(devices, revision)
                raise AdapterError("REVISION_STALE", "Device revision must advance monotonically")
            # Desired credentials are durable even if runtime application fails, so
            # recovery cannot silently reactivate a revoked credential from old plans.
            atomic_write(
                self.root / "device-state.json",
                json.dumps({"devices": devices, "revision": revision}),
            )
            try:
                return self.executor.sync_devices(devices, revision)
            except Exception:
                for info in self.status():
                    if info["role"] == "ingress" and info["state"] == State.ACTIVE:
                        tx = self._read(info["id"])
                        tx["state"] = State.DEGRADED
                        tx["error"] = "DEVICE_SYNC_FAILED"
                        self._save(tx)
                raise

    def block_ingress(self):
        with self.lock("ingress"):
            # Persist the desired block before stopping. A reboot must not replay
            # a formerly ACTIVE plan after the API could not supply new secrets.
            for info in self.status():
                if info["role"] == "ingress" and info["state"] in {State.ACTIVE, State.DEGRADED}:
                    tx = self._read(info["id"])
                    tx["state"] = State.DEGRADED
                    tx["error"] = "EXPLICIT_INGRESS_BLOCK"
                    self._save(tx)
            self.executor._cmd(["systemctl", "stop", "wayvia-ingress.service"])
            return {"status": "blocked"}

    def monitor(self, transaction_id):
        tx = self._read(transaction_id)
        with self.lock(tx["role"]):
            tx = self._read(transaction_id)
            if tx["state"] not in {State.ACTIVE, State.DEGRADED}:
                return self.public(tx)
            plan = Plan.model_validate(tx["plan"])
            tx["evidence"] = self.verifier(self.executor, plan.probes)
            if not self._healthy(tx["evidence"], plan):
                self.executor.protect(plan)
                tx["state"] = State.DEGRADED
            self._save(tx)
            return self.public(tx)
