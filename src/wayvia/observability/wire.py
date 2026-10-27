"""Bounded local IPC and process I/O. Errors contain no input or tool output."""

import json
import os
import selectors
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

from wayvia.observability.models import LogError, canonical

MAX_RESPONSE = 3145728


def exchange_process(argv, payload, limit, timeout):
    try:
        child = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except OSError:
        raise LogError("LOG_TRANSFER_UNAVAILABLE", status=503) from None
    output = bytearray()
    try:
        child.stdin.write(payload + b"\n")
        child.stdin.close()
        end = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while time.monotonic() < end:
                if not selector.select(0.1):
                    continue
                block = os.read(child.stdout.fileno(), 16384)
                if not block:
                    break
                output.extend(block)
                if len(output) > limit:
                    raise LogError("LOG_TRANSFER_LIMIT", status=503)
            else:
                raise LogError("LOG_TRANSFER_TIMEOUT", status=503)
        if child.wait(timeout=max(0.01, end - time.monotonic())) != 0:
            raise LogError("LOG_TRANSFER_UNAVAILABLE", status=503)
        return bytes(output)
    except (OSError, subprocess.TimeoutExpired):
        raise LogError("LOG_TRANSFER_UNAVAILABLE", status=503) from None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        child.stdout.close()
        if not child.stdin.closed:
            child.stdin.close()


def receive(sock, limit):
    header = b""
    while len(header) < 4:
        block = sock.recv(4 - len(header))
        if not block:
            raise LogError("LOG_IPC_INVALID", status=503)
        header += block
    size = struct.unpack("!I", header)[0]
    if not 0 < size <= limit:
        raise LogError("LOG_IPC_LIMIT", status=503)
    body = bytearray()
    while len(body) < size:
        block = sock.recv(min(65536, size - len(body)))
        if not block:
            raise LogError("LOG_IPC_INVALID", status=503)
        body.extend(block)
    return json.loads(body)


def transmit(sock, value):
    payload = canonical(value).encode()
    if len(payload) > MAX_RESPONSE:
        raise LogError("LOG_IPC_LIMIT", status=503)
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def peer_uid(sock):
    if not hasattr(socket, "SO_PEERCRED"):
        raise LogError("PEER_AUTH_UNSUPPORTED", status=503)
    return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def bind(path, kind, mode):
    path = Path(path).absolute()
    if not path.parent.is_dir() or any(p.is_symlink() for p in (path, *path.parents)):
        raise LogError("LOG_SOCKET_PATH")
    if path.exists():
        # A service restart may leave its socket. Never unlink other objects/owners.
        import stat

        value = path.stat()
        if not stat.S_ISSOCK(value.st_mode) or value.st_uid != os.geteuid():
            raise LogError("LOG_SOCKET_PATH")
        path.unlink()
    sock = socket.socket(socket.AF_UNIX, kind)
    sock.bind(str(path))
    os.chmod(path, mode)
    return sock


class BrokerClient:
    def __init__(self, path):
        self.path = str(path)

    def call(self, operation, **values):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(3)
                sock.connect(self.path)
                transmit(sock, {"operation": operation, **values})
                result = receive(sock, MAX_RESPONSE)
                if "error" in result:
                    raise LogError(result["error"], status=503)
                return result["result"]
        except (OSError, ValueError, KeyError):
            raise LogError("LOG_BROKER_UNAVAILABLE", status=503) from None

    def query(self, filters=None, *, cursor=None, limit=100):
        return self.call("query", filters=filters or {}, cursor=cursor, limit=limit)

    def sources(self):
        return self.call("sources")

    def policy(self):
        return self.call("policy")

    def update_policy(self, changes, expected_revision):
        return self.call("policy_update", changes=changes, expected_revision=expected_revision)

    def rollups(self, since, until):
        return self.call("rollups", since=since, until=until)


def broker(store, path, admin_uid, stop):
    """Two bounded workers. Root can read policy only; it cannot query browsing logs."""
    slots = threading.BoundedSemaphore(2)
    sock = bind(path, socket.SOCK_STREAM, 0o666)
    sock.listen(4)
    sock.settimeout(0.5)

    def handle(client):
        with client:
            client.settimeout(3)
            try:
                uid = peer_uid(client)
                request = receive(client, 16384)
                operation = request.pop("operation", None)
                if uid != admin_uid and not (uid == 0 and operation == "policy"):
                    raise LogError("LOG_IPC_DENIED", status=403)
                fields = {
                    "query": {"filters", "cursor", "limit"},
                    "sources": set(),
                    "policy": set(),
                    "policy_update": {"changes", "expected_revision"},
                    "rollups": {"since", "until"},
                }
                if operation not in fields or set(request) != fields[operation]:
                    raise LogError("LOG_IPC_INVALID")
                fn = (
                    store.update_policy
                    if operation == "policy_update"
                    else getattr(store, operation)
                )
                transmit(client, {"result": fn(**request)})
            except Exception as exc:
                try:
                    transmit(
                        client,
                        {"error": exc.code if isinstance(exc, LogError) else "LOG_IPC_INVALID"},
                    )
                except (OSError, LogError):
                    pass
            finally:
                slots.release()

    try:
        while not stop.is_set():
            try:
                client, _ = sock.accept()
            except TimeoutError:
                continue
            if slots.acquire(blocking=False):
                threading.Thread(target=handle, args=(client,), daemon=True).start()
            else:
                client.close()
    finally:
        sock.close()
        Path(path).unlink(missing_ok=True)
