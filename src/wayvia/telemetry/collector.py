"""Passive rolling collector under an explicit ObservationRun lease.

Peer triggers cross only an explicitly configured private file channel. There is
no built-in SSH, outbound control request, scheduler, service start or health edit.
"""

import json
import os
import signal
import sqlite3
import threading
import time
from itertools import islice
from pathlib import Path

from wayvia.adapters.linux import atomic_write
from wayvia.evidence import context, sanitize_context, token
from wayvia.observation_run import ObservationError, private, read_private
from wayvia.telemetry.records import EvidenceError, canonical, digest, stamp, timestamp
from wayvia.telemetry.sources import Sampler, audit_events, collect_journal
from wayvia.telemetry.store import DiagnosticStore


def read_transaction(path):
    private(path)
    if path.stat().st_size > 2097152:
        raise EvidenceError("EVIDENCE_TRANSACTION_TOO_LARGE")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise EvidenceError("EVIDENCE_TRANSACTION_INVALID")
    return value


def trigger_channel(store, inbox, outbox, now):
    if inbox is None or outbox is None:
        return "unavailable"
    for directory in (Path(inbox), Path(outbox)):
        if any(p.is_symlink() for p in (directory, *directory.parents)):
            raise EvidenceError("EVIDENCE_UNSAFE_PATH")
        private(directory, directory=True)
    incoming = list(islice(Path(inbox).iterdir(), 129))
    if len(incoming) > 128:
        return "truncated"
    for path in incoming:
        if token(path.name, r"[a-f0-9]{32}\.json") is None:
            continue
        value = read_private(path)
        store.import_trigger(value, now=now)
    own = {t["incident_id"]: t for t in store.triggers()}
    outgoing = list(islice(Path(outbox).iterdir(), 129))
    if len(outgoing) > 128:
        return "truncated"
    for path in outgoing:
        if token(path.name, r"[a-f0-9]{32}\.json") is None:
            raise EvidenceError("EVIDENCE_TRIGGER_OUTBOX_INVALID")
        previous = read_private(path)
        if previous.get("revision") != store.metadata["revision"]:
            raise EvidenceError("EVIDENCE_TRIGGER_OUTBOX_INVALID")
        if previous.get("path_id") != store.metadata["path_id"] or previous.get(
            "origin_role"
        ) not in {"ingress", "exit"}:
            raise EvidenceError("EVIDENCE_TRIGGER_OUTBOX_INVALID")
        if path.stem not in own:
            path.unlink()  # Only this configured outbox's expired own trigger files.
    for ident, value in own.items():
        path = Path(outbox) / (ident + ".json")
        if not path.exists() or read_private(path) != value:
            atomic_write(path, canonical(value))
    return "ok"


class RollingCollector:
    def __init__(
        self,
        run,
        store: DiagnosticStore,
        *,
        peer,
        port,
        interface,
        agent_root,
        audit_db=None,
        inbox=None,
        outbox=None,
        sampler=None,
    ):
        self.run, self.store = run, store
        self.agent_root = Path(agent_root)
        self.audit_db, self.inbox, self.outbox = audit_db, inbox, outbox
        self.metadata = {key: run.contract[key] for key in ("role", "revision", "generation")}
        self.metadata["run_id"] = run.run_id
        self.sampler = sampler or Sampler(
            role=self.metadata["role"], peer=peer, port=port, interface=interface, run_id=run.run_id
        )

    def step(self):
        """One software-testable iteration. It never invokes health/recovery methods."""
        if self.run.remaining() <= 0:
            return "deadline"
        started, cpu_started = time.monotonic(), time.process_time()
        role, generation = self.metadata["role"], self.metadata["generation"]
        runtime = read_private(self.agent_root / (role + "-runtime.json"))
        transaction = read_transaction(self.agent_root / (generation + ".json"))
        if (
            runtime.get("transaction_id") != generation
            or transaction.get("revision") != self.metadata["revision"]
        ):
            return "generation_changed"
        progress = self.store.progress()
        row = self.sampler.sample(self.metadata, budget=self.run)
        now = timestamp(row["observed_at"])
        previous_at = progress.get("last_sample")
        if previous_at and now - timestamp(previous_at) > self.store.policy.interval_seconds * 1.5:
            self.store.append(
                {
                    **context(**self.metadata),
                    "observed_at": row["observed_at"],
                    "type": "event",
                    "kind": "collector_gap",
                    "source": "collector",
                    "gap_start": previous_at,
                    "gap_end": row["observed_at"],
                },
                now=now,
            )
        since = progress.get("journal_since", now - self.store.policy.buffer_seconds)
        events, status = collect_journal(since=since - 5, until=now, role=role, budget=self.run)
        row["sources"]["journal"] = status
        # Failed/truncated reads keep the checkpoint, making the missing interval
        # visible and eligible for a later bounded reread.
        if status == "ok":
            self.store.checkpoint("journal_since", now)
        for event in events:
            self.store.append(event, now=now)
        if self.audit_db:
            try:
                events, status = audit_events(
                    self.audit_db,
                    since=progress.get("audit_since", now - self.store.policy.buffer_seconds),
                    until=now,
                    role=role,
                )
                for event in events:
                    self.store.append(event, now=now)
                if status == "ok":
                    self.store.checkpoint("audit_since", now)
                row["sources"]["audit"] = status
            except (OSError, sqlite3.Error, ValueError):
                row["sources"]["audit"] = "unavailable"
        else:
            row["sources"]["audit"] = "unavailable"
        # The current journal snapshot provides an explicit fallback when the
        # system journal is absent. It cannot reconstruct intervening transitions.
        monitored = transaction.get("monitored_at")
        valid_until = transaction.get("monitor_valid_until")
        if (
            monitored
            and valid_until
            and transaction.get("state") in {"ACTIVE", "DEGRADED"}
            and now - self.store.policy.buffer_seconds <= timestamp(monitored) <= now
            and timestamp(valid_until) > now
        ):
            degraded = transaction["state"] == "DEGRADED"
            self.store.append(
                {
                    **context(**self.metadata, observed_at=monitored),
                    "monotonic_ms": None,
                    "boot_id": None,
                    "software_version": None,
                    "producer_digest": None,
                    "event_id": digest([generation, "health", monitored, transaction["state"]]),
                    "type": "event",
                    "kind": "health_degraded" if degraded else "health_healthy",
                    "source": "snapshot",
                    "status": "degraded" if degraded else "healthy",
                },
                now=now,
            )
        for index, evidence in enumerate(transaction.get("evidence", [])[:7]):
            if not isinstance(evidence, dict):
                continue
            at = evidence.get("observed_at")
            if at is None or timestamp(at) < now - self.store.policy.buffer_seconds:
                continue
            event = {
                **sanitize_context(evidence),
                **evidence,
                **self.metadata,
                "type": "event",
                "kind": "probe_failed" if evidence.get("status") != "healthy" else "probe_healthy",
                "source": "snapshot",
                "event_id": digest([generation, index, at, evidence.get("status")]),
            }
            self.store.append(event, now=now)
        try:
            row["sources"]["peer"] = trigger_channel(self.store, self.inbox, self.outbox, now)
        except (ObservationError, OSError, EvidenceError):
            row["sources"]["peer"] = "error"
        # Measure payload cost after adding source completeness, without storing
        # the raw journal bytes or diagnostic command arguments.
        row["collector"]["duration_ms"] = (time.monotonic() - started) * 1000
        row["collector"]["cpu_ms"] = (time.process_time() - cpu_started) * 1000
        row["collector"]["payload_bytes"] = len(canonical(row))
        self.store.append(row, now=now)
        self.store.checkpoint("last_sample", row["observed_at"])
        return "continue"

    def run_loop(self):
        wake = threading.Event()

        def interrupted(*unused):
            self.run.interrupted = True
            wake.set()

        handlers = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
        reason = "deadline"
        try:
            while self.run.remaining() > 0:
                started = time.monotonic()
                try:
                    reason = self.step()
                except EvidenceError as exc:
                    reason = (
                        "storage_unavailable"
                        if exc.code == "EVIDENCE_STORAGE_UNAVAILABLE"
                        else "collector_error"
                    )
                except Exception:
                    reason = "collector_error"
                if reason != "continue":
                    break
                wake.wait(
                    max(
                        0,
                        min(
                            self.run.remaining(),
                            self.store.policy.interval_seconds - (time.monotonic() - started),
                        ),
                    )
                )
            if reason == "continue":
                reason = "deadline"
            if self.run.interrupted:
                reason = "interrupted"
            reason = self.run.finish(reason)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
        return 0 if reason in {"deadline", "user_stop"} else 5


def run_collector(
    *,
    run_id,
    until,
    role,
    generation,
    peer,
    port,
    interface,
    path_id,
    policy,
    resume=False,
    audit_db=None,
    inbox=None,
    outbox=None,
    measure_clock_offset=False,
):
    import platform

    from wayvia.evidence import producer_digest
    from wayvia.observation_run import ObservationRun, contract_digest

    if platform.system() != "Linux":
        raise EvidenceError("ENVIRONMENT_UNSUPPORTED")
    if (
        os.geteuid() != 0
        or role not in {"ingress", "exit"}
        or token(generation, r"[a-f0-9]{32}") is None
        or token(path_id, r"[a-zA-Z0-9-]{1,64}") is None
    ):
        raise EvidenceError("EVIDENCE_COLLECTOR_ARGUMENTS")
    root = Path("/var/lib/wayvia/agent")
    transaction = read_transaction(root / (generation + ".json"))
    revision = transaction.get("revision")
    if token(revision, r"[a-f0-9]{64}") is None:
        raise EvidenceError("EVIDENCE_REVISION_INVALID")
    # Validate scope before creating a run. No sampler command is executed here.
    sampler = Sampler(
        role=role,
        peer=peer,
        port=port,
        interface=interface,
        run_id=run_id,
        measure_clock_offset=measure_clock_offset,
    )
    contract = {
        "mode": "rolling_passive",
        "role": role,
        "revision": revision,
        "generation": generation,
        "until": stamp(timestamp(until)),
        "max_bytes": 1048576,
        "sample_interval_seconds": policy.interval_seconds,
        "policy": policy.model_dump(),
        "path_id": path_id,
        "producer_digest": producer_digest(),
        "inputs_digest": contract_digest(
            [
                str(sampler.peer),
                port,
                interface,
                str(audit_db) if audit_db else None,
                str(inbox) if inbox else None,
                str(outbox) if outbox else None,
                measure_clock_offset,
            ]
        ),
    }
    with ObservationRun("/var/lib/wayvia/diagnostics", run_id, contract, resume=resume) as run:
        metadata = {key: contract[key] for key in ("role", "revision", "generation", "path_id")}
        metadata["run_id"] = run_id
        store = DiagnosticStore(run.directory / "rolling", metadata, policy)
        return RollingCollector(
            run,
            store,
            peer=peer,
            port=port,
            interface=interface,
            agent_root=root,
            audit_db=audit_db,
            inbox=inbox,
            outbox=outbox,
            sampler=sampler,
        ).run_loop()
