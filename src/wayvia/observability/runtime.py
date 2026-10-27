"""Independent bounded collector. No privileged network operations or production defaults."""

import queue
import signal
import socket
import struct
import threading
import time
import uuid
from pathlib import Path

from wayvia.observability.files import locked, policy, read_json, write_json
from wayvia.observability.http import nginx_event
from wayvia.observability.models import Event, LogError, event
from wayvia.observability.store import LogStore
from wayvia.observability.transport import Spool, accept, ssh_offer
from wayvia.observability.wire import bind, broker


def collect(
    root, socket_path, broker_path, host_id, role, producer_uids, admin_uid, *, peer=None, stop=None
):
    if not hasattr(socket, "SO_PASSCRED"):
        raise LogError("PEER_AUTH_UNSUPPORTED", status=503)
    store = LogStore(root, create=True)
    if policy(root).hosts.get(host_id) != role:
        raise LogError("LOG_SOURCE_INVALID")
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
    with locked(Path(root) / "collector.lock", nonblocking=True):
        sock = bind(socket_path, socket.SOCK_DGRAM, 0o666)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        sock.settimeout(0.5)
        pending = queue.Queue(maxsize=2048)  # <=8 MiB after strict event projection.
        failures = {"dropped": 0, "invalid": 0, "storage": False, "dropped_batches": 0}
        epoch = uuid.uuid4().hex

        def receive():
            while not stop.is_set():
                try:
                    raw, ancillary, flags, _ = sock.recvmsg(8192, socket.CMSG_SPACE(12))
                    credentials = [
                        value
                        for level, kind, value in ancillary
                        if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS
                    ]
                    if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(credentials) != 1:
                        raise ValueError()
                    uid = struct.unpack("3i", credentials[0])[1]
                    allowed = producer_uids.get(uid, set())
                    if not allowed:
                        raise ValueError()
                    if raw.startswith(b"{"):
                        item = Event.model_validate_json(raw)
                    else:
                        item = nginx_event(raw, host_id, role)
                    if item.source not in allowed or item.host_id != host_id or item.role != role:
                        raise ValueError()
                    if not policy(root).desired_enabled:
                        continue
                    pending.put_nowait(item)
                except queue.Full:
                    failures["dropped"] += 1
                except TimeoutError:
                    continue
                except Exception:
                    if not stop.is_set():
                        failures["invalid"] += 1

        receiver = threading.Thread(target=receive, daemon=True)
        reader = threading.Thread(
            target=broker, args=(store, broker_path, admin_uid, stop), daemon=True
        )
        receiver.start()
        reader.start()
        spool = Spool(Path(root) / "spool", host_id, role, max_bytes=50331648)
        last_state = 0
        reported_drops = 0
        peer_worker = (
            threading.Thread(target=pull_peer, args=(store, peer, epoch, stop), daemon=True)
            if peer
            else None
        )
        if peer_worker:
            peer_worker.start()
        try:
            while not stop.wait(0.1):
                enabled = policy(root).desired_enabled
                items = []
                while len(items) < 190:
                    try:
                        items.append(pending.get_nowait())
                    except queue.Empty:
                        break
                if not enabled:
                    if time.monotonic() - last_state >= 5:
                        last_state = time.monotonic()
                        store.prune()
                        spool.offer()
                        write_json(
                            Path(root) / "collector-state.json",
                            failures | {"at": time.time(), "queue": 0, "disabled": True},
                        )
                    continue
                if failures["dropped"] > reported_drops:
                    items.append(
                        event(
                            host_id,
                            role,
                            "transfer",
                            epoch,
                            "gap",
                            code="queue_full",
                            lost_count=failures["dropped"] - reported_drops,
                        )
                    )
                    reported_drops = failures["dropped"]
                try:
                    if items:
                        # Journal only validated records, never the original input.
                        spool.append(items)
                    offer = spool.offer()
                    failures["dropped_batches"] = offer["dropped_batches"]
                    offered = offer["batch"]
                    if offered:
                        store.ingest(offered["events"])
                        spool.offer({"sequence": offered["sequence"], "digest": offered["digest"]})
                    failures["storage"] = False
                except Exception:
                    failures["storage"] = True
                    if items:
                        failures["dropped"] += len(items)
                if time.monotonic() - last_state >= 5:
                    last_state = time.monotonic()
                    try:
                        write_json(
                            Path(root) / "collector-state.json",
                            failures | {"at": time.time(), "queue": pending.qsize()},
                        )
                        store.prune()
                    except Exception:
                        failures["storage"] = True
        finally:
            stop.set()
            sock.close()
            receiver.join(timeout=1)
            reader.join(timeout=1)
            Path(socket_path).unlink(missing_ok=True)


def source_loop(root, host_id, role, *, control_db=None, stop=None):
    from wayvia.observability.system import SystemSource, audit

    spool = Spool(Path(root) / "source-spool", host_id, role, max_bytes=16777216)
    source = SystemSource(host_id, role)
    stop = stop or threading.Event()
    store = LogStore(root, create=True)
    with locked(Path(root) / "source.lock", nonblocking=True):
        while not stop.is_set():
            spool.offer()
            store.prune()
            if policy(root).desired_enabled:
                checkpoint = read_json(spool.state)
                events, cursor = source.collect(checkpoint["cursor"], disk_path=root)
                audit_id = checkpoint["audit"]
                if control_db:
                    try:
                        rows, audit_id = audit(control_db, audit_id, host_id, role)
                        events.extend(rows)
                        events.append(event(host_id, role, "audit", "control-v4", "heartbeat"))
                    except Exception:
                        events.append(
                            event(
                                host_id,
                                role,
                                "audit",
                                "control-v4",
                                "source_error",
                                code="source_unavailable",
                            )
                        )
                # Chunk checkpoints only after every preceding chunk is durable.
                try:
                    for start in range(0, len(events), 200):
                        final = start + 200 >= len(events)
                        spool.append(
                            events[start : start + 200],
                            checkpoint={"cursor": cursor, "audit": audit_id} if final else None,
                        )
                    if role == "ingress":
                        while (offered := spool.offer()["batch"]) is not None:
                            store.ingest(offered["events"])
                            spool.offer({k: offered[k] for k in ("sequence", "digest", "epoch")})
                except Exception:
                    pass  # Spool keeps acknowledged cursor; source freshness expires.
            stop.wait(5)


def pull_peer(store, peer, epoch, stop, fetch=ssh_offer):
    """Drain faster than the producer, outside the local persistence loop."""
    while not stop.is_set():
        try:
            if store.policy()["desired_enabled"]:
                for _ in range(12):
                    if stop.is_set() or not store.policy()["desired_enabled"]:
                        break
                    offset = store.offset("transfer:" + peer["host_id"])
                    ack = (
                        {k: offset[k] for k in ("sequence", "digest", "epoch")} if offset else None
                    )
                    offer = fetch(peer["ssh"], ack)
                    if offer["host_id"] != peer["host_id"] or offer["role"] != "exit":
                        raise LogError("LOG_SOURCE_INVALID")
                    if offer["batch"]:
                        accept(store, offer["batch"], peer["host_id"], "exit")
                    else:
                        store.ingest(
                            [event(peer["host_id"], "exit", "transfer", epoch, "heartbeat")]
                        )
                        break
        except Exception:
            try:
                store.ingest(
                    [
                        event(
                            peer["host_id"],
                            "exit",
                            "transfer",
                            epoch,
                            "source_error",
                            code="source_unavailable",
                        )
                    ]
                )
            except Exception:
                pass
        stop.wait(15)
