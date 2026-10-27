"""Explicit opt-in commands. Initialization never starts collection or changes network state."""

import json
import os
import signal
import threading
import uuid
from pathlib import Path

import typer

from wayvia.observability.models import LogError


def register(app, guarded, output):
    logs = typer.Typer(no_args_is_help=True, help="Private passive metadata logs")
    admin = typer.Typer(no_args_is_help=True, help="Private loopback administrator UI")
    app.add_typer(logs, name="logs")
    app.add_typer(admin, name="admin")

    @logs.command("init")
    @guarded
    def initialize(root: Path):
        from wayvia.observability.store import LogStore

        output(LogStore(root, create=True).policy())

    @logs.command("policy")
    @guarded
    def policy(root: Path, enabled: bool | None = None):
        from wayvia.observability.store import LogStore

        store = LogStore(root)
        value = store.policy()
        output(
            store.update_policy({"desired_enabled": enabled}, value["revision"])
            if enabled is not None
            else value
        )

    @logs.command("query")
    @guarded
    def query(
        root: Path,
        since: float,
        until: float,
        kind: str | None = None,
        device_id: str | None = None,
    ):
        from wayvia.observability.store import LogStore

        output(
            LogStore(root).query(
                {
                    k: v
                    for k, v in {
                        "since": since,
                        "until": until,
                        "kind": kind,
                        "device_id": device_id,
                    }.items()
                    if v is not None
                }
            )
        )

    @logs.command("prune")
    @guarded
    def prune(root: Path):
        from wayvia.observability.store import LogStore

        LogStore(root).prune()
        output({"pruned": True})

    @logs.command("render-nginx")
    @guarded
    def nginx(source: Path, destination: Path, socket_path: str = "/run/wayvia-log/events.sock"):
        from wayvia.observability.nginx import render

        if destination.exists() or destination.is_symlink() or source.stat().st_size > 65536:
            raise LogError("LOG_RENDER_PATH")
        result = render(source.read_text(), socket_path)
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(result)
        output({"rendered": True, "applied": False})

    @logs.command("collect")
    @guarded
    def collect(root: Path, config: Path):
        import pwd

        from wayvia.observability.files import read_json
        from wayvia.observability.runtime import collect as run

        value = read_json(config)
        if set(value) - {
            "host_id",
            "role",
            "admin_user",
            "api_user",
            "nginx_user",
            "socket",
            "broker",
            "peer",
        }:
            raise LogError("LOG_CONFIG_INVALID")
        producers = {0: {"connections", "audit"}}
        for key, source in (("api_user", "api"), ("nginx_user", "nginx")):
            if value.get(key):
                producers.setdefault(pwd.getpwnam(value[key]).pw_uid, set()).add(source)
        run(
            root,
            value["socket"],
            value["broker"],
            value["host_id"],
            value["role"],
            producers,
            pwd.getpwnam(value["admin_user"]).pw_uid,
            peer=value.get("peer"),
        )

    @logs.command("source")
    @guarded
    def source(root: Path, host_id: str, role: str):
        from wayvia.observability.runtime import source_loop

        source_loop(root, host_id, role, stop=shutdown())

    @logs.command("relay")
    @guarded
    def relay(root: Path, host_id: str, role: str):
        import sys

        from wayvia.observability.files import policy
        from wayvia.observability.transport import Spool

        if os.environ.get("SSH_ORIGINAL_COMMAND") not in {None, "wayvia-log-relay-v1"}:
            raise LogError("LOG_RELAY_COMMAND")
        raw = sys.stdin.buffer.readline(4097)
        if len(raw) > 4096:
            raise LogError("LOG_ACK_INVALID")
        request = json.loads(raw)
        if set(request) != {"ack"} or not policy(root).desired_enabled or role != "exit":
            raise LogError("LOG_RELAY_DISABLED")
        output(
            Spool(root / "source-spool", host_id, role, max_bytes=33554432).offer(request["ack"])
        )

    @logs.command("relay-ssh")
    @guarded
    def relay_ssh():
        """Forced SSH command for a separate, unprivileged transfer identity."""
        import subprocess

        if os.environ.get("SSH_ORIGINAL_COMMAND") != "wayvia-log-relay-v1":
            raise LogError("LOG_RELAY_COMMAND")
        try:
            result = subprocess.run(
                [
                    "sudo",
                    "-n",
                    "-u",
                    "wayvia-observer",
                    "--",
                    "/opt/wayvia/.venv/bin/wayvia",
                    "logs",
                    "relay",
                    "/var/lib/wayvia-observer",
                    "jp",
                    "exit",
                ],
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise LogError("LOG_TRANSFER_UNAVAILABLE", status=503) from None
        if result.returncode:
            raise typer.Exit(result.returncode)

    @logs.command("core")
    @guarded
    def core(
        host_id: str,
        broker_path: Path,
        socket_path: Path,
        secret_file: Path = Path("/var/lib/wayvia/agent/usage-api-secret"),
        control_db: Path = Path("/var/lib/wayvia/control/metadata.db"),
    ):
        from wayvia.observability.connections import stream
        from wayvia.observability.http import send
        from wayvia.observability.models import event
        from wayvia.observability.system import audit, devices
        from wayvia.observability.wire import BrokerClient
        from wayvia.telemetry.store import secure_path

        stop = shutdown()
        client = BrokerClient(broker_path)
        epoch = uuid.uuid4().hex

        def enabled():
            return not stop.is_set() and client.policy()["desired_enabled"]

        dropped = {}
        emission_lock = threading.Lock()

        def emit(item):
            with emission_lock:
                lost = dropped.get(item.source, 0)
                if lost and send(
                    socket_path,
                    event(
                        host_id,
                        "ingress",
                        item.source,
                        epoch,
                        "gap",
                        code="queue_full",
                        lost_count=lost,
                    ),
                ):
                    dropped[item.source] = 0
                accepted = send(socket_path, item)
                if not accepted:
                    dropped[item.source] = dropped.get(item.source, 0) + 1
                return accepted

        def audits():
            after = 0
            while not stop.wait(5):
                try:
                    if not enabled():
                        continue
                    rows, next_id = audit(control_db, after, host_id, "ingress")
                    complete = all(emit(item) for item in rows)
                    if complete:
                        after = next_id
                        emit(event(host_id, "ingress", "audit", epoch, "heartbeat"))
                except Exception:
                    emit(
                        event(
                            host_id,
                            "ingress",
                            "audit",
                            epoch,
                            "source_error",
                            code="source_unavailable",
                        )
                    )

        worker = threading.Thread(target=audits, daemon=True)
        worker.start()
        while not stop.is_set():
            try:
                if enabled():
                    path = secure_path(secret_file)
                    if path.stat().st_size > 4096:
                        raise LogError("LOG_SECRET_INVALID")
                    secret = path.read_text()
                    stream(secret, host_id, lambda: devices(control_db), emit, enabled)
            except Exception:
                emit(
                    event(
                        host_id,
                        "ingress",
                        "connections",
                        epoch,
                        "source_error",
                        code="source_unavailable",
                    )
                )
            stop.wait(5)

    @admin.command("init")
    @guarded
    def admin_init(root: Path):
        from wayvia.admin.state import AdminState

        AdminState(root, create=True)
        output({"initialized": True, "listening": False})

    @admin.command("login-code")
    @guarded
    def login_code(root: Path):
        from wayvia.admin.state import AdminState

        output({"code": AdminState(root).code(), "expires_in": 60, "one_time": True})

    @admin.command("serve")
    @guarded
    def serve(root: Path, broker_path: Path, control_token_file: Path, port: int = 8790):
        import uvicorn

        from wayvia.admin.app import create_app
        from wayvia.admin.control import ControlGateway
        from wayvia.admin.state import AdminState
        from wayvia.observability.wire import BrokerClient
        from wayvia.telemetry.store import secure_path

        if not 1024 <= port <= 65535:
            raise LogError("ADMIN_PRIVATE_PORT")
        path = secure_path(control_token_file)
        if path.stat().st_size > 4096:
            raise LogError("ADMIN_CONTROL_SECRET")
        application = create_app(
            AdminState(root),
            BrokerClient(broker_path),
            ControlGateway(path.read_text().strip()),
            origin=f"http://127.0.0.1:{port}",
        )
        uvicorn.run(
            application,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_level="critical",
            limit_concurrency=16,
            timeout_keep_alive=5,
        )


def shutdown():
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    return stop
