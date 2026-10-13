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
    evidence_seconds = 90
    safety_blocks = {"EXPLICIT_INGRESS_BLOCK", "DEVICE_SYNC_FAILED"}

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
        result = {
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
                "predecessor_id",
                "device_revision",
                "monitored_at",
                "monitor_valid_until",
                "submitted_digest",
                "effective_digest",
            )
        }
        runtime = self._runtime(tx["role"])
        result["current"] = runtime.get("transaction_id") == tx["id"]
        result["generation"] = tx["id"]
        result["verification_scope"] = self._scope(tx["role"])
        result["blocked_reason"] = (
            runtime.get("blocked_reason") if result["current"] else tx.get("blocked_reason")
        )
        return result

    def _records(self, role=None):
        records = [
            json.loads(path.read_text())
            for path in sorted(self.root.glob("*.json"))
            if re.fullmatch(r"[a-f0-9]{32}\.json", path.name)
        ]
        return [tx for tx in records if role is None or tx["role"] == role]

    def _runtime(self, role):
        path = self.root / f"{role}-runtime.json"
        if path.exists():
            return json.loads(path.read_text())
        # Upgrade journals from before the explicit running-generation pointer.
        # An unconfirmed change owns the actual resources ahead of its predecessor.
        candidates = [
            tx
            for tx in self._records(role)
            if tx["state"] not in {State.SUPERSEDED, State.ROLLED_BACK}
        ]
        pending = [tx for tx in candidates if tx["state"] not in {State.ACTIVE, State.DEGRADED}]
        current = max(
            pending or candidates,
            key=lambda tx: tx.get("confirmed_at", tx["plan"]["created_at"]),
            default=None,
        )
        return {
            "transaction_id": current["id"] if current else None,
            "blocked_reason": (
                current.get("error")
                if current and current.get("error") in self.safety_blocks
                else None
            ),
        }

    def _write_runtime(self, role, runtime):
        atomic_write(self.root / f"{role}-runtime.json", json.dumps(runtime, sort_keys=True))

    def _set_current(self, role, transaction_id):
        runtime = self._runtime(role)
        runtime["transaction_id"] = transaction_id
        self._write_runtime(role, runtime)

    def _current(self, role):
        transaction_id = self._runtime(role).get("transaction_id")
        return self._read(transaction_id) if transaction_id else None

    def _set_block(self, role, reason):
        runtime = self._runtime(role)
        runtime["blocked_reason"] = reason
        self._write_runtime(role, runtime)

    @staticmethod
    def _scope(role):
        return "datapath" if role == "ingress" else "component_readiness"

    def _observe(self, tx, plan, *, until=None):
        started = time.monotonic()
        if self.verifier is verify:
            evidence = self.verifier(self.executor, plan.probes, deadline_monotonic=until)
        else:
            # Explicitly injected test verifiers retain the simple two-argument API.
            evidence = self.verifier(self.executor, plan.probes)
        finished = datetime.now(UTC)
        for item in evidence:
            item.setdefault("observed_at", finished.isoformat())
            item.setdefault("duration_ms", round((time.monotonic() - started) * 1000, 3))
            observed = datetime.fromisoformat(item["observed_at"])
            item["valid_until"] = (observed + timedelta(seconds=self.evidence_seconds)).isoformat()
            item["revision"] = plan.revision
            item["generation"] = tx["id"]
            item.setdefault("scope", self._scope(tx["role"]))
        tx["evidence"] = evidence
        return self._healthy(evidence, plan)

    def _verify_ready(self, tx, plan, *, deadline=None):
        until = time.monotonic() + self.verify_seconds
        probe_budget = until if self.verify_seconds else None
        if deadline is not None:
            deadline_budget = time.monotonic() + max(
                0, (deadline - datetime.now(UTC)).total_seconds()
            )
            until = min(until, deadline_budget)
            probe_budget = min(probe_budget, deadline_budget) if probe_budget else deadline_budget
        while True:
            # Even test mode performs one complete observation; the zero retry window
            # is not a zero budget for the real production verifier.
            if self._observe(tx, plan, until=probe_budget):
                return True
            if time.monotonic() >= until:
                return False
            time.sleep(min(0.2, max(0, until - time.monotonic())))

    def status(self):
        return [self.public(tx) for tx in self._records()]

    def apply(self, plan):
        if not isinstance(plan, Plan):
            plan = Plan.model_validate(plan)
        with self.lock(plan.role):
            runtime = self._runtime(plan.role)
            if runtime.get("blocked_reason"):
                raise AdapterError(
                    "INGRESS_BLOCKED", "Reconcile the protected credentials before deployment"
                )
            if any(
                t["role"] == plan.role
                and t["state"]
                not in {State.ACTIVE, State.DEGRADED, State.SUPERSEDED, State.ROLLED_BACK}
                for t in self.status()
            ):
                raise AdapterError(
                    "TRANSACTION_PENDING",
                    "Role already has an unconfirmed or unrecovered transaction",
                )
            plan.check(self.executor.baseline(plan.actions))
            submitted_digest = plan.digest
            # Credential reconciliation is a separate monotonic stream from network
            # plans. Never replay a password snapshot captured before a revocation.
            plan = plan.model_copy(
                update={"actions": tuple(self._credential_action(a) for a in plan.actions)}
            ).seal()
            self.executor.preflight(plan)
            predecessor = self._current(plan.role)
            credentials = self._credentials() if plan.role == "ingress" else None
            tx = {
                "id": uuid.uuid4().hex,
                "role": plan.role,
                "revision": plan.revision,
                "state": State.APPLYING,
                "plan": plan.model_dump(mode="json"),
                "steps": [],
                "predecessor_id": predecessor["id"] if predecessor else None,
                "predecessor_state": predecessor["state"] if predecessor else None,
                "submitted_digest": submitted_digest,
                "effective_digest": plan.digest,
                "device_revision": credentials["revision"] if credentials else None,
                "deadline": (
                    datetime.now(UTC) + timedelta(seconds=plan.confirm_seconds)
                ).isoformat(),
            }
            self._save(tx)
            try:
                # Timer installation succeeds before any mutating action starts.
                self.timer.arm(tx["id"], plan.confirm_seconds, self.root)
                self._set_current(plan.role, tx["id"])
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
                if not self._verify_ready(
                    tx, plan, deadline=datetime.fromisoformat(tx["deadline"])
                ):
                    raise AdapterError("PROBE_FAILED", "One or more required path probes failed")
                if datetime.now(UTC) >= datetime.fromisoformat(tx["deadline"]):
                    raise AdapterError("APPLY_DEADLINE", "Apply exceeded its confirmation window")
                tx["state"] = State.AWAITING_CONFIRM
                self._save(tx)
            except Exception as exc:
                tx["error"] = getattr(exc, "code", "APPLY_FAILED")
                tx["apply_error"] = tx["error"]
                details = getattr(exc, "details", {})
                if isinstance(details, dict):
                    tx["apply_error_details"] = {
                        key: details[key]
                        for key in ("operation", "status", "errorCode")
                        if key in details and type(details[key]) in {str, int, bool}
                    }
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
            if self._runtime(tx["role"]).get("transaction_id") != tx["id"]:
                raise AdapterError("TRANSACTION_NOT_CURRENT", "Another generation owns this role")
            if self._runtime(tx["role"]).get("blocked_reason"):
                raise AdapterError("INGRESS_BLOCKED", "Credential protection must be reconciled")
            if datetime.now(UTC) >= datetime.fromisoformat(tx["deadline"]):
                self._rollback(tx)
                raise AdapterError(
                    "CONFIRM_EXPIRED", "Confirmation window expired; rollback was initiated"
                )
            plan = Plan.model_validate(tx["plan"])
            deadline = datetime.fromisoformat(tx["deadline"])
            until = time.monotonic() + max(0, (deadline - datetime.now(UTC)).total_seconds())
            healthy = self._observe(tx, plan, until=until)
            # A rollback timer waiting for this role lock cannot enforce a deadline
            # on our behalf. Check again immediately before committing ACTIVE.
            if datetime.now(UTC) >= deadline:
                self._rollback(tx)
                raise AdapterError("CONFIRM_EXPIRED", "Probes exceeded the confirmation window")
            if not healthy:
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
                    and info["state"] in {State.ACTIVE, State.DEGRADED}
                ):
                    previous = self._read(info["id"])
                    previous["state"] = State.SUPERSEDED
                    previous["superseded_by"] = tx["id"]
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
                self._restore_predecessor(tx)
            except Exception as exc:
                failures.append(getattr(exc, "code", "RESTORE_FAILED"))
        tx["state"] = State.RECOVERY_REQUIRED if failures else State.ROLLED_BACK
        tx["recovery_failures"] = failures
        if failures:
            tx["error"] = "RECOVERY_REQUIRED"
            self._set_current(tx["role"], tx["id"])
            try:
                self.executor.protect(Plan.model_validate(tx["plan"]))
            except Exception:
                tx["protection"] = "unconfirmed"
        self._save(tx)
        try:
            self.timer.cancel(tx["id"])
        except AdapterError:
            pass

    def _restore_predecessor(self, tx):
        predecessor_id = tx.get("predecessor_id")
        if not predecessor_id:
            self._set_current(tx["role"], None)
            return
        previous = self._read(predecessor_id)
        plan = Plan.model_validate(previous["plan"])
        blocked = self._runtime(tx["role"]).get("blocked_reason")
        previous["state"] = tx.get("predecessor_state") or State.ACTIVE
        previous.pop("superseded_by", None)
        # The restored deployment needs fresh path evidence; old successful probes
        # cannot certify the outcome of a different transaction's rollback.
        if blocked or not self._verify_ready(previous, plan):
            previous["state"] = State.DEGRADED
            previous["error"] = blocked or "RESTORED_PATH_FAILED"
            self.executor.protect(plan)
        else:
            previous["state"] = State.ACTIVE
            previous.pop("error", None)
        previous["monitored_at"] = None
        previous["monitor_valid_until"] = None
        credentials = self._credentials() if previous["role"] == "ingress" else None
        previous["device_revision"] = credentials["revision"] if credentials else None
        self._save(previous)
        # Persist predecessor restoration before ending the successor's rollback.
        # Replaying a ROLLING_BACK journal after a crash remains idempotent.
        self._set_current(tx["role"], predecessor_id)

    def rollback(self, transaction_id, *, only_unconfirmed=False):
        tx = self._read(transaction_id)
        with self.lock(tx["role"]):
            tx = self._read(transaction_id)
            if only_unconfirmed and (
                tx["state"] == State.SUPERSEDED
                or tx["state"] in {State.ACTIVE, State.DEGRADED}
                and tx.get("confirmed_at")
            ):
                return self.public(tx)
            if tx["state"] == State.SUPERSEDED:
                raise AdapterError(
                    "TRANSACTION_SUPERSEDED",
                    "A newer configuration depends on this transaction; "
                    "create a fresh rollback plan",
                )
            if tx["state"] == State.ROLLED_BACK or only_unconfirmed and tx["state"] == State.ACTIVE:
                return self.public(tx)
            current = self._runtime(tx["role"]).get("transaction_id")
            before_takeover = not tx["steps"] and tx.get("predecessor_id") == current
            if current != tx["id"] and tx["state"] != State.ROLLING_BACK and not before_takeover:
                raise AdapterError(
                    "TRANSACTION_NOT_CURRENT",
                    "Cannot roll back resources owned by a newer generation",
                )
            self._rollback(tx)
            return self.public(tx)

    def recover(self, *, rebuild_active=False):
        results = []
        for role in ("ingress", "exit"):
            with self.lock(role):
                current = self._current(role)
                if current:
                    self._set_current(role, current["id"])
                # Finish incomplete rollback first, including a crash after it had
                # already restored the predecessor pointer but before its final save.
                pending = [
                    tx
                    for tx in self._records(role)
                    if tx["state"]
                    not in {State.ACTIVE, State.SUPERSEDED, State.ROLLED_BACK, State.DEGRADED}
                ]
                for tx in sorted(pending, key=lambda t: t["plan"]["created_at"], reverse=True):
                    self._rollback(tx)
                    results.append(self.public(tx))
                current = self._current(role)
                if not current:
                    continue
                blocked = self._runtime(role).get("blocked_reason")
                if blocked:
                    # A process may have crashed after persisting a security block
                    # but before the stop command. Recovery must enforce it again.
                    current["state"] = State.DEGRADED
                    current["error"] = blocked
                    current["evidence"] = []
                    self._save(current)
                    try:
                        self.executor.protect(Plan.model_validate(current["plan"]))
                    except Exception:
                        current["protection"] = "unconfirmed"
                        self._save(current)
                    continue
                # A crash after confirming a successor but before marking its old
                # transaction SUPERSEDED must not replay both at boot.
                if current["state"] in {State.ACTIVE, State.DEGRADED}:
                    for previous in self._records(role):
                        if previous["id"] != current["id"] and previous["state"] in {
                            State.ACTIVE,
                            State.DEGRADED,
                        }:
                            previous["state"] = State.SUPERSEDED
                            previous["superseded_by"] = current["id"]
                            self._save(previous)
                if (
                    rebuild_active
                    and current["state"] in {State.ACTIVE, State.DEGRADED}
                    and not self._runtime(role).get("blocked_reason")
                ):
                    self._rebuild(current)
                    results.append(self.public(current))
        return results

    def _rebuild(self, tx):
        plan = Plan.model_validate(tx["plan"])
        tx["state"] = State.DEGRADED
        tx["monitored_at"] = None
        tx["monitor_valid_until"] = None
        self._save(tx)
        try:
            self.executor.protect(plan)
            # Keep the guard in place throughout reconnection and establish it first
            # after a reboot. Listeners only forward through this protected path.
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
                self.executor.apply(self._credential_action(action))
            if not self._verify_ready(tx, plan):
                raise AdapterError("PROBE_FAILED", "Recovery probes failed")
            tx["state"] = State.ACTIVE
            tx.pop("error", None)
            credentials = self._credentials() if tx["role"] == "ingress" else None
            tx["device_revision"] = credentials["revision"] if credentials else None
        except Exception as exc:
            tx["error"] = getattr(exc, "code", "RECOVERY_FAILED")
            try:
                self.executor.protect(plan)
            except Exception:
                tx["protection"] = "unconfirmed"
        self._save(tx)

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
            action = action.model_copy(update={"content": json.dumps(config)})
        if action.kind == "install_config" and action.resource == "ingress.json":
            prepare = getattr(self.executor, "prepare_ingress", None)
            if prepare:
                action = prepare(action)
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
                if revision != current["revision"] or devices != current["devices"]:
                    raise AdapterError(
                        "REVISION_STALE", "Device revision must advance monotonically"
                    )
            # Desired credentials are durable even if runtime application fails, so
            # recovery cannot silently reactivate a revoked credential from old plans.
            atomic_write(
                self.root / "device-state.json",
                json.dumps({"devices": devices, "revision": revision}),
            )
            try:
                result = self.executor.sync_devices(devices, revision)
            except Exception as exc:
                tx = self._current("ingress")
                if tx:
                    self._set_block("ingress", "DEVICE_SYNC_FAILED")
                    if tx["state"] in {State.ACTIVE, State.DEGRADED}:
                        tx["state"] = State.DEGRADED
                    tx["error"] = "DEVICE_SYNC_FAILED"
                    tx["evidence"] = []
                    self._save(tx)
                elif (
                    isinstance(exc, FileNotFoundError)
                    and exc.filename is not None
                    and getattr(self.executor, "config_dir", None) is not None
                    and Path(exc.filename) == self.executor.config_dir / "ingress.json"
                ):
                    # A validated, durably saved desired snapshot can resolve a
                    # predeployment credential block without claiming an active
                    # listener. Otherwise a repaired secret could never bootstrap:
                    # sync needs an installed config while apply refuses the block.
                    self._set_block("ingress", None)
                    raise AdapterError(
                        "INGRESS_NOT_DEPLOYED",
                        "Desired credentials are saved; deploy the initial ingress to apply them",
                        details={"revision": revision, "status": "pending"},
                    ) from exc
                raise
            # Only a successful explicit reconciliation of the current monotonic
            # credential snapshot clears a credential/security block. Neither the
            # boot recovery nor the periodic monitor can do this automatically.
            self._set_block("ingress", None)
            tx = self._current("ingress")
            if tx:
                tx["device_revision"] = revision
                tx["evidence"] = []
                tx["monitored_at"] = None
                tx["monitor_valid_until"] = None
                tx.pop("blocked_reason", None)
                if tx.get("error") in self.safety_blocks:
                    tx.pop("error", None)
                self._save(tx)
            return result

    def block_ingress(self):
        with self.lock("ingress"):
            # Persist the desired block before stopping. A reboot must not replay
            # a formerly ACTIVE plan after the API could not supply new secrets.
            self._set_block("ingress", "EXPLICIT_INGRESS_BLOCK")
            tx = self._current("ingress")
            if tx:
                if tx["state"] in {State.ACTIVE, State.DEGRADED}:
                    tx["state"] = State.DEGRADED
                tx["error"] = "EXPLICIT_INGRESS_BLOCK"
                tx["evidence"] = []
                self._save(tx)
            self.executor._cmd(["systemctl", "stop", "wayvia-ingress.service"])
            return {"status": "blocked"}

    def monitor(self, transaction_id):
        tx = self._read(transaction_id)
        with self.lock(tx["role"]):
            tx = self._read(transaction_id)
            runtime = self._runtime(tx["role"])
            if (
                tx["state"] not in {State.ACTIVE, State.DEGRADED}
                or runtime.get("transaction_id") != tx["id"]
            ):
                return self.public(tx)
            plan = Plan.model_validate(tx["plan"])
            if runtime.get("blocked_reason"):
                tx["state"] = State.DEGRADED
                tx["error"] = runtime["blocked_reason"]
                tx["evidence"] = []
            elif tx["state"] == State.DEGRADED:
                self._rebuild(tx)
            else:
                try:
                    healthy = self._observe(tx, plan)
                except Exception as exc:
                    healthy = False
                    tx["evidence"] = []
                    tx["error"] = getattr(exc, "code", "RUNTIME_PROBE_FAILED")
                if not healthy:
                    tx["state"] = State.DEGRADED
                    tx.setdefault("error", "RUNTIME_PROBE_FAILED")
            if tx["state"] == State.DEGRADED:
                self._save(tx)
                try:
                    self.executor.protect(plan)
                except Exception:
                    tx["protection"] = "unconfirmed"
            observed = datetime.now(UTC)
            tx["monitored_at"] = observed.isoformat()
            tx["monitor_valid_until"] = (
                observed + timedelta(seconds=self.evidence_seconds)
            ).isoformat()
            self._save(tx)
            return self.public(tx)
