"""Normal per-account telemetry in an explicitly disposable Linux container only.

No fault injection, routing changes, host access, or reboot tests.
Run with PYTHONPATH including src and locked grpcio/protobuf installed.
"""

import http.server
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import grpc

from wayvia.adapters.singbox.usage import MESSAGES, api_config, snapshot


def ready(port):
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("Disposable listener did not start")


def main():
    if not Path("/.dockerenv").exists():
        raise RuntimeError("Requires a disposable Docker container")
    os.umask(0o077)
    secret = "usage-test-only-" + "a" * 32
    phone, desktop = "a" * 32, "b" * 32
    processes = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(65536))
            self.end_headers()
            for _ in range(64):
                self.wfile.write(b"x" * 1024)
                self.wfile.flush()
                time.sleep(0.02)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 18080), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=localhost",
                    "-addext",
                    "subjectAltName=DNS:localhost",
                    "-keyout",
                    str(root / "key.pem"),
                    "-out",
                    str(root / "cert.pem"),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            config = {
                "log": {"level": "error"},
                "services": [api_config(secret)],
                "inbounds": [
                    {
                        "type": "trojan",
                        "tag": "entry",
                        "listen": "127.0.0.1",
                        "listen_port": 24443,
                        "users": [
                            {"name": phone, "password": "phone-test-only"},
                            {"name": desktop, "password": "desktop-test-only"},
                        ],
                        "tls": {
                            "enabled": True,
                            "certificate_path": str(root / "cert.pem"),
                            "key_path": str(root / "key.pem"),
                        },
                    }
                ],
                "outbounds": [{"type": "direct", "tag": "disposable-target"}],
            }
            configs = [config]
            for index, password in enumerate(["phone-test-only", "desktop-test-only"]):
                configs.append(
                    {
                        "log": {"level": "error"},
                        "inbounds": [
                            {"type": "socks", "listen": "127.0.0.1", "listen_port": 10880 + index}
                        ],
                        "outbounds": [
                            {
                                "type": "trojan",
                                "server": "127.0.0.1",
                                "server_port": 24443,
                                "password": password,
                                "tls": {
                                    "enabled": True,
                                    "server_name": "localhost",
                                    "certificate_path": str(root / "cert.pem"),
                                },
                            }
                        ],
                    }
                )
            for i, config in enumerate(configs):
                path = root / f"{i}.json"
                path.write_text(json.dumps(config))
                subprocess.run(["sing-box", "check", "-c", str(path)], check=True)
                processes.append(
                    subprocess.Popen(
                        ["sing-box", "run", "-c", str(path)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                )
            for port in (24443, 19090, 10880, 10881):
                ready(port)
            with grpc.insecure_channel("127.0.0.1:19090") as channel:
                call = channel.unary_unary(
                    "/daemon.StartedService/GetVersion",
                    response_deserializer=MESSAGES["Version"].FromString,
                )
                try:
                    call(b"", timeout=2)
                    raise AssertionError("Unauthenticated API accepted")
                except grpc.RpcError as error:
                    assert error.code() == grpc.StatusCode.UNAUTHENTICATED
            baseline = snapshot(secret)
            assert baseline["connections"] == []
            results = {}
            for port, device in [(10880, phone), (10881, desktop)]:
                curl = subprocess.Popen(
                    [
                        "curl",
                        "--silent",
                        "--fail",
                        "--max-time",
                        "8",
                        "--noproxy",
                        "",
                        "--socks5-hostname",
                        f"127.0.0.1:{port}",
                        "http://127.0.0.1:18080/",
                        "-o",
                        "/dev/null",
                    ]
                )
                deadline = time.monotonic() + 4
                while True:
                    active = snapshot(secret)
                    if any(
                        c["device_id"] == device and c["closed_at"] is None
                        for c in active["connections"]
                    ):
                        break
                    assert time.monotonic() < deadline, (
                        "No authenticated active connection observed"
                    )
                    time.sleep(0.05)
                assert curl.wait(timeout=10) == 0
                time.sleep(0.1)
                closed = [c for c in snapshot(secret)["connections"] if c["device_id"] == device]
                assert closed and all(c["closed_at"] for c in closed)
                assert sum(c["download"] for c in closed) >= 65536
                assert sum(c["upload"] for c in closed) > 0
                results["phone" if device == phone else "desktop"] = {
                    "attributed": True,
                    "online_observed": True,
                    "closed_observed": True,
                    "upload_bytes": sum(c["upload"] for c in closed),
                    "download_bytes": sum(c["download"] for c in closed),
                }
            print(
                json.dumps(
                    {
                        "scope": "isolated native API normal-path only",
                        "core": "sing-box 1.14.3",
                        "unauthenticated_rejected": True,
                        "devices": results,
                    },
                    indent=2,
                )
            )
    finally:
        server.shutdown()
        for process in processes:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    main()
