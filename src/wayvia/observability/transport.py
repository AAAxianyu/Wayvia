"""Durable projected batches, bounded replay and a fixed SSH forced-command protocol."""

import json
import re
import shutil
import time
import uuid
from pathlib import Path

from wayvia.observability.files import directory, locked, read_json, write_json
from wayvia.observability.models import Event, LogError, canonical, digest

MAX_BATCH = 900000


class Spool:
    def __init__(self, root, host_id, role, *, max_bytes=67108864):
        self.root = directory(root)
        self.host_id, self.role, self.max_bytes = host_id, role, max_bytes
        self.state = self.root / "state.json"
        with locked(self.root / "spool.lock"):
            if not self.state.exists():
                write_json(
                    self.state,
                    {
                        "epoch": uuid.uuid4().hex,
                        "next": 1,
                        "dropped": 0,
                        "cursor": None,
                        "audit": 0,
                    },
                )

    def append(self, events, *, checkpoint=None):
        if len(events) > 200:
            raise LogError("LOG_BATCH_TOO_LARGE")
        with locked(self.root / "spool.lock"):
            state = read_json(self.state)
            body = {
                "version": 1,
                "host_id": self.host_id,
                "role": self.role,
                "epoch": state["epoch"],
                "sequence": state["next"],
                "created_at": time.time(),
                "events": [e.data() for e in events],
            }
            batch = body | {"digest": digest(body)}
            size = len(canonical(batch))
            if size > min(MAX_BATCH, self.max_bytes):
                raise LogError("LOG_BATCH_TOO_LARGE")
            files = sorted(self.root.glob("batch-*.json"))
            total = sum(p.stat().st_size for p in files)
            while files and (total + size > self.max_bytes or len(files) >= 512):
                path = files.pop(0)
                total -= path.stat().st_size
                path.unlink()
                state["dropped"] += 1
            if shutil.disk_usage(self.root).free < max(2097152, size * 2):
                raise LogError("LOG_STORAGE_FULL", status=503)
            write_json(self.root / f"batch-{state['next']:020d}.json", batch)
            state["next"] += 1
            if checkpoint:
                state.update({k: v for k, v in checkpoint.items() if k in {"cursor", "audit"}})
            write_json(self.state, state)
            return batch

    def _expire(self):
        state = read_json(self.state)
        changed = False
        for path in sorted(self.root.glob("batch-*.json")):
            if path.stat().st_mtime < time.time() - 86400:
                path.unlink()
                state["dropped"] += 1
                changed = True
        if changed:
            write_json(self.state, state)

    def offer(self, ack=None):
        with locked(self.root / "spool.lock"):
            if ack is not None:
                if (
                    not isinstance(ack, dict)
                    or set(ack) not in ({"sequence", "digest"}, {"sequence", "digest", "epoch"})
                    or not isinstance(ack["sequence"], int)
                    or not 1 <= ack["sequence"] < 10**20
                ):
                    raise LogError("LOG_ACK_INVALID")
                path = self.root / f"batch-{ack['sequence']:020d}.json"
                if path.exists():
                    batch = read_json(path, MAX_BATCH)
                    if ack.get("epoch", batch["epoch"]) != batch["epoch"]:
                        path = None
                    elif batch["digest"] != ack["digest"]:
                        raise LogError("LOG_ACK_INVALID")
                    if path is not None:
                        path.unlink()
            self._expire()
            files = sorted(self.root.glob("batch-*.json"))
            state = read_json(self.state)
            return {
                "batch": read_json(files[0], MAX_BATCH) if files else None,
                "dropped_batches": state["dropped"],
                "host_id": self.host_id,
                "role": self.role,
            }


def accept(store, batch, host_id, role):
    if not isinstance(batch, dict) or set(batch) != {
        "version",
        "host_id",
        "role",
        "epoch",
        "sequence",
        "created_at",
        "events",
        "digest",
    }:
        raise LogError("LOG_BATCH_INVALID")
    body = {k: v for k, v in batch.items() if k != "digest"}
    if (
        len(canonical(batch)) > MAX_BATCH
        or digest(body) != batch["digest"]
        or batch["version"] != 1
        or batch["host_id"] != host_id
        or batch["role"] != role
    ):
        raise LogError("LOG_BATCH_INVALID")
    if (
        not re.fullmatch(r"[a-f0-9]{32}", str(batch["epoch"]))
        or not isinstance(batch["sequence"], int)
        or not 1 <= batch["sequence"] < 10**20
    ):
        raise LogError("LOG_BATCH_INVALID")
    if len(batch["events"]) > 200 or batch["created_at"] > time.time() + 60:
        raise LogError("LOG_BATCH_INVALID")
    items = [Event.model_validate(e) for e in batch["events"]]
    if any(
        e.host_id != host_id or e.role != role or e.source not in {"system", "audit"} for e in items
    ):
        raise LogError("LOG_SOURCE_INVALID")
    key = f"transfer:{host_id}"
    previous = store.offset(key)
    ack = {"sequence": batch["sequence"], "digest": batch["digest"], "epoch": batch["epoch"]}
    if (
        previous
        and previous["epoch"] == batch["epoch"]
        and batch["sequence"] <= previous["sequence"]
    ):
        if batch["sequence"] == previous["sequence"] and batch["digest"] == previous["digest"]:
            return ack
        raise LogError("LOG_BATCH_REPLAY")
    from wayvia.observability.models import event

    if (
        not previous
        or previous["epoch"] != batch["epoch"]
        or batch["sequence"] != previous["sequence"] + 1
    ):
        store.ingest([event(host_id, role, "transfer", batch["epoch"], "gap", code="sequence_gap")])
    store.ingest(items, checkpoint=(key, ack | {"epoch": batch["epoch"]}))
    return ack


def ssh_offer(config, ack=None):
    """Only an explicit pinned host, key and forced command; no ambient SSH config."""
    if set(config) != {"host", "port", "user", "identity", "known_hosts"}:
        raise LogError("LOG_PEER_CONFIG")
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", config["host"])
        or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", config["user"])
        or not 1 <= config["port"] <= 65535
    ):
        raise LogError("LOG_PEER_CONFIG")
    for name in ("identity", "known_hosts"):
        from wayvia.telemetry.store import secure_path

        path = secure_path(Path(config[name]))
        if (
            not path.is_absolute()
            or not path.is_file()
            or path.is_symlink()
            or path.stat().st_mode & 0o077
        ):
            raise LogError("LOG_PEER_CONFIG")
    argv = [
        "ssh",
        "-F",
        "/dev/null",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=1",
        "-o",
        "UserKnownHostsFile=" + config["known_hosts"],
        "-i",
        config["identity"],
        "-p",
        str(config["port"]),
        config["user"] + "@" + config["host"],
        "wayvia-log-relay-v1",
    ]
    # Fixed helper emits at most MAX_BATCH. Read through a bounded temporary file
    # would still allow disk exhaustion; selectors enforce the pipe bound instead.
    from wayvia.observability.wire import exchange_process

    raw = exchange_process(argv, canonical({"ack": ack}).encode(), MAX_BATCH + 4096, 15)
    return json.loads(raw)
