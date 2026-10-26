"""Durable lifecycle for explicitly authorized, deadline-bound observation runs.

An OS signal is an interruption, never proof of user intent. Only request_stop
creates the durable stop marker. Restarting identical arguments may resume an
interrupted run before its original deadline; legacy/terminal runs are refused.
"""

import fcntl
import hashlib
import json
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path

from wayvia.adapters.linux import atomic_write
from wayvia.evidence import boot_id, context, number, safe_time, token


class ObservationError(RuntimeError):
    """A fixed public code; never include raw paths or OS exception messages."""


class SizeLimitError(ObservationError):
    pass


def private(path, *, directory=False):
    """Only the owning uid can supply lifecycle state. Refuse symlinks/hardlinks."""
    info = path.lstat()
    if (
        info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        or (not directory and info.st_nlink != 1)
    ):
        raise ObservationError("UNSAFE_OBSERVATION_PATH")


def read_private(path):
    private(path)
    if path.stat().st_size > 65536:
        raise ObservationError("INVALID_OBSERVATION_STATE")
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise ObservationError("INVALID_OBSERVATION_STATE") from None


def run_directory(root, run_id):
    if token(run_id, r"[a-zA-Z0-9-]{1,64}") is None:
        raise ObservationError("INVALID_RUN_ID")
    root = Path(root)
    # Parents can be shared system directories, but never redirect through a symlink.
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ObservationError("UNSAFE_OBSERVATION_PATH")
    private(root, directory=True)
    path = root / run_id
    private(path, directory=True)
    return path


def request_stop(root, run_id):
    directory = run_directory(root, run_id)
    manifest = read_private(directory / "run.json")
    if manifest.get("run_id") != run_id or manifest.get("schema_version") != 1:
        raise ObservationError("INVALID_OBSERVATION_STATE")
    # Separate from the collector's lifetime lock, so a live process can be stopped.
    # The marker is monotonic: neither start nor resume ever removes it.
    stop = directory / "stop.json"
    if stop.exists():
        read_private(stop)
    else:
        atomic_write(
            stop,
            json.dumps({"requested_at": datetime.now(UTC).isoformat(), "reason": "user_stop"})
            + "\n",
        )
    return {"run_id": run_id, "status": "stop_requested"}


class Writer:
    def __init__(self, path, limit=96 * 1024 * 1024, *, used=0, metadata=None):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.file = os.fdopen(fd, "w", encoding="utf-8")
        self.used = used
        self.limit = limit
        self.metadata = metadata

    def put(self, row):
        if self.metadata is not None:
            envelope = context(**self.metadata)
            # Preserve the actual observation time of asynchronously completed controls.
            envelope["observed_at"] = (
                safe_time(row.get("started_at") or row.get("at")) or envelope["observed_at"]
            )
            if number(row.get("monotonic")) is not None:
                envelope["monotonic_ms"] = round(row["monotonic"] * 1000, 3)
            row = {**row, **envelope}
        text = json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n"
        size = len(text.encode())
        if size > 65536 or self.used + size > self.limit:
            raise SizeLimitError("OBSERVATION_SIZE_LIMIT")
        self.file.write(text)
        self.file.flush()
        os.fsync(self.file.fileno())
        self.used += size


class ObservationRun:
    def __init__(self, root, run_id, contract, *, now=None, resume=False):
        self.root = Path(root)
        self.run_id = run_id
        self.contract = contract
        self.now = now or (lambda: datetime.now(UTC))
        self.lock = None
        self.writer = None
        self.interrupted = False
        self.allow_resume = resume

    def __enter__(self):
        try:
            return self._start()
        except BaseException:
            self.close()
            raise

    def _start(self):
        if token(self.run_id, r"[a-zA-Z0-9-]{1,64}") is None:
            raise ObservationError("INVALID_RUN_ID")
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise ObservationError("UNSAFE_OBSERVATION_PATH")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        private(self.root, directory=True)
        self.directory = self.root / self.run_id
        try:
            self.directory.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            created = False
        private(self.directory, directory=True)
        lock_path = self.directory / "run.lock"
        if lock_path.exists() or lock_path.is_symlink():
            private(lock_path)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(fd, "r+")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ObservationError("OBSERVATION_ALREADY_RUNNING") from None
        self.until = datetime.fromisoformat(self.contract["until"])
        if self.until.tzinfo is None:
            raise ObservationError("INVALID_OBSERVATION_DEADLINE")
        seconds = (self.until - self.now()).total_seconds()
        if not 0 < seconds <= 86400:
            raise ObservationError("OBSERVATION_DEADLINE")
        self.deadline = time.monotonic() + seconds
        self.boot = boot_id()
        manifest_path = self.directory / "run.json"
        self.resumed = not created
        if created:
            self.manifest = {
                "schema_version": 1,
                "run_id": self.run_id,
                "contract": self.contract,
                "created_at": self.now().isoformat(),
            }
            atomic_write(manifest_path, json.dumps(self.manifest) + "\n")
        else:
            if not manifest_path.exists():
                raise ObservationError("LEGACY_OR_INCOMPLETE_RUN")
            self.manifest = read_private(manifest_path)
            if self.manifest != {
                "schema_version": 1,
                "run_id": self.run_id,
                "contract": self.contract,
                "created_at": self.manifest.get("created_at"),
            }:
                raise ObservationError("OBSERVATION_CONTRACT_CHANGED")
        if self.stop_requested():
            raise ObservationError("OBSERVATION_STOPPED")
        if not created and not self.allow_resume:
            raise ObservationError("OBSERVATION_RESUME_REQUIRED")
        state_path = self.directory / "state.json"
        previous = read_private(state_path) if state_path.exists() else {}
        if previous.get("status") not in {None, "running", "interrupted"}:
            raise ObservationError("OBSERVATION_TERMINAL")
        if previous:
            last_at = safe_time(previous.get("at"))
            if last_at is None or self.now() < datetime.fromisoformat(last_at):
                raise ObservationError("OBSERVATION_CLOCK_REGRESSION")
            if self.boot is not None and previous.get("boot_id") == self.boot:
                original_deadline = number(previous.get("deadline_monotonic"))
                if original_deadline is None:
                    raise ObservationError("INVALID_OBSERVATION_STATE")
                self.deadline = min(self.deadline, original_deadline)
        if self.remaining() <= 0:
            raise ObservationError("OBSERVATION_DEADLINE")
        self.used, self.last_at, self.partial_tails = self._existing_evidence()
        if self.last_at and self.now() < datetime.fromisoformat(self.last_at):
            raise ObservationError("OBSERVATION_CLOCK_REGRESSION")
        completions = sorted(self.directory.glob("result-*.json"))
        if completions:
            latest = read_private(completions[-1])
            segments = list(self.directory.glob("samples*.jsonl"))
            # A crash after saving terminal evidence but before the mutable state
            # checkpoint must not resurrect a finished run.
            if latest.get("segment") == len(segments) and latest.get("reason") != "interrupted":
                raise ObservationError("OBSERVATION_TERMINAL")
        if self.used >= self.contract["max_bytes"]:
            raise SizeLimitError("OBSERVATION_SIZE_LIMIT")
        # Never alter an earlier segment, including its incomplete final line.
        self.segment = len(list(self.directory.glob("samples*.jsonl"))) + 1
        if self.segment > 1024:
            raise SizeLimitError("OBSERVATION_SEGMENT_LIMIT")
        name = "samples.jsonl" if self.segment == 1 else f"samples-{self.segment:06d}.jsonl"
        self.writer = Writer(
            self.directory / name,
            self.contract["max_bytes"],
            used=self.used,
            metadata={k: self.contract[k] for k in ("role", "revision", "generation")}
            | {"run_id": self.run_id},
        )
        self._state("running")
        self.writer.put(
            {
                "type": "resume" if self.resumed else "start",
                "at": self.now().isoformat(),
                "until": self.contract["until"],
                "segment": self.segment,
                "gap_after": self.last_at,
                "preserved_partial_tails": self.partial_tails,
                "max_bytes": self.contract["max_bytes"],
                "sample_interval_seconds": self.contract["sample_interval_seconds"],
                "scope": "diagnostics_only_no_health_decision_or_mutation",
            }
        )
        return self

    def _existing_evidence(self):
        used, last_at, tails = 0, None, 0
        for path in sorted(
            self.directory.glob("samples*.jsonl"), key=lambda p: (p.name != "samples.jsonl", p.name)
        ):
            if (
                path.name != "samples.jsonl"
                and token(path.name, r"samples-[0-9]{6}\.jsonl") is None
            ):
                raise ObservationError("INVALID_EVIDENCE_SEGMENT")
            private(path)
            used += path.stat().st_size
            if used > self.contract["max_bytes"]:
                raise SizeLimitError("OBSERVATION_SIZE_LIMIT")
            with path.open("rb") as stream:
                while line := stream.readline(65537):
                    if len(line) > 65536:
                        raise ObservationError("INVALID_EVIDENCE_SEGMENT")
                    if not line.endswith(b"\n"):
                        tails += 1
                        break
                    try:
                        row = json.loads(line)
                        at = safe_time(row.get("at"))
                        if at is None:
                            raise ValueError()
                        last_at = at
                    except (ValueError, AttributeError):
                        raise ObservationError("INVALID_EVIDENCE_SEGMENT") from None
        return used, last_at, tails

    def _state(self, status, reason=None):
        state = {
            "status": status,
            "reason": reason,
            "at": self.now().isoformat(),
            "boot_id": self.boot,
            "deadline_monotonic": self.deadline,
        }
        atomic_write(self.directory / "state.json", json.dumps(state) + "\n")

    def stop_requested(self):
        path = self.directory / "stop.json"
        if path.exists() or path.is_symlink():
            read_private(path)
            return True
        return False

    def remaining(self):
        if self.interrupted or self.stop_requested():
            return 0
        return max(
            0, min(self.deadline - time.monotonic(), (self.until - self.now()).total_seconds())
        )

    def finish(self, reason):
        if self.stop_requested():
            reason = "user_stop"
        status = {
            "deadline": "completed",
            "user_stop": "stopped",
            "interrupted": "interrupted",
        }.get(reason, "failed")
        result = {
            "type": "end",
            "at": self.now().isoformat(),
            "reason": reason,
            "segment": self.segment,
            "bytes_written": self.writer.used,
        }
        # Archive every completion before updating the mutable latest-result pointer.
        destination = self.directory / f"result-{self.segment:06d}.json"
        if destination.exists():
            raise ObservationError("RESULT_ALREADY_EXISTS")
        atomic_write(destination, json.dumps(result) + "\n")
        latest = self.directory / "result.json"
        if latest.exists() or latest.is_symlink():
            private(latest)
        atomic_write(latest, json.dumps(result) + "\n")
        self._state(status, reason)
        return reason

    def close(self):
        if self.writer is not None:
            self.writer.file.close()
        if self.lock is not None:
            self.lock.close()

    def __exit__(self, *args):
        self.close()


def contract_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
