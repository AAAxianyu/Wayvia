"""Steady-state four-process software resource budget; synthetic native RPC, no routing."""

import concurrent.futures
import http.server
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import grpc
import httpx
from google.protobuf.empty_pb2 import Empty

from wayvia.admin.state import AdminState
from wayvia.observability.connections import MESSAGES
from wayvia.observability.files import write_json
from wayvia.observability.http import send
from wayvia.observability.models import event
from wayvia.observability.store import LogStore


def metrics(pid):
    values = Path(f"/proc/{pid}/stat").read_text().split()
    cpu = (int(values[13]) + int(values[14])) / os.sysconf("SC_CLK_TCK")
    rss = 0
    pss = 0
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            rss = int(line.split()[1]) * 1024
    for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
        if line.startswith("Pss:"):
            pss = int(line.split()[1]) * 1024
    return cpu, rss, pss


def main():
    if not Path("/.dockerenv").exists():
        raise RuntimeError("Disposable Linux container required")
    os.umask(0o077)
    devices = [f"{i:032x}" for i in range(1, 6)]

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            data = json.dumps({"devices": [], "dataplane": {"status": "simulated"}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    web_server = http.server.ThreadingHTTPServer(("127.0.0.1", 8787), Handler)
    threading.Thread(target=web_server.serve_forever, daemon=True).start()
    grpc_server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=4))

    def updates(request, context):
        yield MESSAGES["Events"](reset=True)
        while context.is_active():
            time.sleep(1)
            yield MESSAGES["Events"]()

    methods = {
        "GetVersion": grpc.unary_unary_rpc_method_handler(
            lambda r, c: MESSAGES["Version"](version="1.14.3", apiVersion=4),
            request_deserializer=Empty.FromString,
            response_serializer=MESSAGES["Version"].SerializeToString,
        ),
        "GetStartedAt": grpc.unary_unary_rpc_method_handler(
            lambda r, c: MESSAGES["StartedAt"](startedAt=1700000000000),
            request_deserializer=Empty.FromString,
            response_serializer=MESSAGES["StartedAt"].SerializeToString,
        ),
        "SubscribeConnections": grpc.unary_stream_rpc_method_handler(
            updates,
            request_deserializer=MESSAGES["Request"].FromString,
            response_serializer=MESSAGES["Events"].SerializeToString,
        ),
    }
    grpc_server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler("daemon.StartedService", methods),)
    )
    grpc_server.add_insecure_port("127.0.0.1:19090")
    grpc_server.start()
    processes = []
    with tempfile.TemporaryDirectory() as name:
        base = Path(name)
        logs = LogStore(base / "logs", create=True)
        logs.update_policy({"desired_enabled": True, "min_free_bytes": 0}, 1)
        state = AdminState(base / "admin", create=True)
        token = base / "control-token"
        token.write_text("synthetic-control-token-" + "a" * 40)
        core_secret = base / "native-secret"
        core_secret.write_text("synthetic-native-secret-" + "b" * 40)
        db = base / "metadata.db"
        with sqlite3.connect(db) as con:
            con.executescript(
                "CREATE TABLE devices(id TEXT); "
                "CREATE TABLE audit(id INTEGER, action TEXT, object_id TEXT, "
                "result TEXT, created_at REAL);"
            )
            con.executemany("INSERT INTO devices VALUES(?)", [(d,) for d in devices])
        sock, broker = base / "events.sock", base / "query.sock"
        config = base / "collector.json"
        write_json(
            config,
            {
                "host_id": "ali",
                "role": "ingress",
                "admin_user": "root",
                "api_user": "root",
                "nginx_user": None,
                "socket": str(sock),
                "broker": str(broker),
            },
        )
        commands = {
            "collector": ["logs", "collect", str(logs.root), str(config)],
            "source": ["logs", "source", str(logs.root), "ali", "ingress"],
            "core": [
                "logs",
                "core",
                "ali",
                str(broker),
                str(sock),
                "--secret-file",
                str(core_secret),
                "--control-db",
                str(db),
            ],
            "admin": ["admin", "serve", str(state.root), str(broker), str(token)],
        }
        try:
            for role, args in commands.items():
                child = subprocess.Popen(
                    [sys.executable, "-c", "from wayvia.cli import app; app()", *args],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                processes.append((role, child))
            ready = time.monotonic() + 10
            while True:
                assert all(p.poll() is None for _, p in processes), "A daemon exited during startup"
                try:
                    with socket.create_connection(("127.0.0.1", 8790), timeout=0.1):
                        break
                except OSError:
                    assert time.monotonic() < ready
                    time.sleep(0.1)
            with httpx.Client(
                base_url="http://127.0.0.1:8790", trust_env=False, timeout=3
            ) as client:
                response = client.post(
                    "/admin/session",
                    json={"code": state.code()},
                    headers={"Origin": "http://127.0.0.1:8790"},
                )
                assert response.status_code == 200, response.text
                headers = {
                    "Origin": "http://127.0.0.1:8790",
                    "X-CSRF-Token": response.json()["csrf"],
                }
                before = {role: metrics(p.pid)[0] for role, p in processes}
                start = time.monotonic()
                peaks = {role: {"rss": 0, "pss": 0} for role in commands}
                for i in range(100):
                    assert send(
                        sock,
                        event(
                            "ali",
                            "ingress",
                            "connections",
                            "budget",
                            "connection",
                            device_id=devices[i % 5],
                            identity_source="credential_only",
                            core_epoch="budget",
                            connection_id=str(i),
                            lifecycle="active",
                            target_ip="198.51.100.1",
                            target_port=443,
                            upload_bytes=100,
                            download_bytes=200,
                        ),
                    )
                    if i % 10 == 0:
                        result = client.post(
                            "/admin/api/events",
                            json={"filters": {"kind": "connection"}},
                            headers=headers,
                        )
                        assert result.status_code == 200, result.text
                        for role, p in processes:
                            assert p.poll() is None
                            _, rss, pss = metrics(p.pid)
                            peaks[role]["rss"] = max(peaks[role]["rss"], rss)
                            peaks[role]["pss"] = max(peaks[role]["pss"], pss)
                    time.sleep(0.1)
                elapsed = time.monotonic() - start
                cpu = sum(metrics(p.pid)[0] - before[role] for role, p in processes)
                rss = sum(v["rss"] for v in peaks.values())
                pss = sum(v["pss"] for v in peaks.values())
                report = {
                    "scope": "four_real_cli_processes_synthetic_rpc_and_events",
                    "duration_seconds": round(elapsed, 3),
                    "connections": 100,
                    "devices": 5,
                    "peaks": peaks,
                    "sum_peak_rss_bytes": rss,
                    "sum_peak_pss_bytes": pss,
                    "cpu_seconds": round(cpu, 3),
                    "cpu_fraction_one_core": round(cpu / elapsed, 4),
                    "memory_budget_bytes": 268435456,
                    "cpu_budget_fraction": 0.25,
                    "passed": rss < 268435456 and cpu / elapsed < 0.25,
                }
                print(json.dumps(report), flush=True)
                assert report["passed"]
        finally:
            for _, p in processes:
                p.terminate()
            for _, p in processes:
                try:
                    p.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
            grpc_server.stop(0)
            web_server.shutdown()
            web_server.server_close()


if __name__ == "__main__":
    main()
