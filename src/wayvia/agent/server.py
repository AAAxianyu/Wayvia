"""Root agent's closed protocol, authenticated with Linux SO_PEERCRED."""

import json
import os
import platform
import socket
import stat
import struct
from pathlib import Path

from pydantic import ValidationError

from wayvia.adapters.base import AdapterError
from wayvia.adapters.linux import LinuxExecutor
from wayvia.agent.client import MAX_MESSAGE
from wayvia.agent.models import Plan
from wayvia.agent.transactions import TransactionManager
from wayvia.errors import WayviaError


def peer_allowed(connection, allowed_uid):
    if platform.system() != "Linux" or not hasattr(socket, "SO_PEERCRED"):
        raise AdapterError(
            "PEER_AUTH_UNSUPPORTED", "SO_PEERCRED is required for privileged agent access"
        )
    _, uid, _ = struct.unpack(
        "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    )
    return uid in {0, allowed_uid}


def dispatch(manager, request):
    operation = request.get("operation")
    permitted = {
        "apply": {"operation", "plan"},
        "confirm": {"operation", "transaction_id"},
        "rollback": {"operation", "transaction_id"},
        "status": {"operation"},
        "recover": {"operation"},
        "baseline": {"operation", "plan"},
        "block_ingress": {"operation"},
        "sync_devices": {"operation", "devices", "revision"},
        "device_usage": {"operation"},
    }
    if operation not in permitted or set(request) != permitted[operation]:
        raise AdapterError("OPERATION_FORBIDDEN", "Request is outside the fixed agent protocol")
    if operation == "apply":
        return manager.apply(Plan.model_validate(request["plan"]))
    if operation == "baseline":
        plan = Plan.model_validate(request["plan"])
        return {"baseline": manager.executor.baseline(plan.actions)}
    if operation == "confirm":
        return manager.confirm(request["transaction_id"])
    if operation == "rollback":
        return manager.rollback(request["transaction_id"])
    if operation == "status":
        return manager.status()
    if operation == "device_usage":
        return manager.executor.device_usage()
    if operation == "recover":
        return manager.recover(rebuild_active=True)
    if operation == "sync_devices":
        return manager.sync_devices(request["devices"], request["revision"])
    if operation == "block_ingress":
        return manager.block_ingress()
    raise AdapterError("OPERATION_FORBIDDEN", "Unsupported operation")


def serve_agent(
    socket_path="/run/wayvia/agent.sock",
    journal_dir="/var/lib/wayvia/agent",
    artifacts_dir="/etc/wayvia",
    allowed_uid=0,
):
    executor = LinuxExecutor(config_dir=artifacts_dir, state_dir=journal_dir)
    executor.require_environment()
    manager = TransactionManager(executor, journal_dir)
    manager.recover()  # Reboot rebuild is handled by the independent systemd unit.
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if path.is_symlink():
        raise AdapterError("UNSAFE_PATH", "Agent socket path cannot be a symlink")
    if path.exists():
        existing = path.lstat()
        if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != 0:
            raise AdapterError("SOCKET_CONFLICT", "An unowned resource occupies the socket path")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                raise AdapterError("SOCKET_CONFLICT", "Another agent owns the active socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        os.chmod(path, 0o660)
        listener.listen(16)
        try:
            while True:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(5)
                    try:
                        if not peer_allowed(connection, allowed_uid):
                            raise AdapterError("PEER_FORBIDDEN", "Local peer UID is not authorized")
                        data = b""
                        while not data.endswith(b"\n"):
                            block = connection.recv(65536)
                            if not block:
                                raise AdapterError("REQUEST_INCOMPLETE", "Incomplete agent message")
                            data += block
                            if len(data) > MAX_MESSAGE:
                                raise AdapterError(
                                    "REQUEST_TOO_LARGE", "Agent message exceeds bounded size"
                                )
                        request = json.loads(data)
                        if not isinstance(request, dict):
                            raise AdapterError("REQUEST_INVALID", "Agent request must be an object")
                        result = {"ok": True, "result": dispatch(manager, request)}
                    except WayviaError as exc:
                        result = {"ok": False, "error": exc.as_dict()}
                    except (ValueError, ValidationError):
                        result = {
                            "ok": False,
                            "error": {
                                "code": "REQUEST_INVALID",
                                "message": "Invalid agent request",
                                "details": {},
                            },
                        }
                    except Exception:
                        result = {
                            "ok": False,
                            "error": {
                                "code": "AGENT_FAILED",
                                "message": "Agent request failed; inspect local recovery state",
                                "details": {},
                            },
                        }
                    connection.sendall(json.dumps(result).encode() + b"\n")
        finally:
            path.unlink(missing_ok=True)
