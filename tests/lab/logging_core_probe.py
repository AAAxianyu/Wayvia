"""Normal per-account telemetry in an explicitly disposable Linux container only.

No fault injection, routing changes, host access, or reboot tests.
Run with PYTHONPATH including src and locked grpcio/protobuf installed.
"""

import http.server
import json
import os
import socket
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import grpc

from wayvia.adapters.singbox.usage import MESSAGES, api_config, snapshot
from wayvia.observability.connections import stream


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
            self.send_header("Content-Length", str(33554432 if self.path == "/bulk" else 65536))
            self.end_headers()
            if self.path == "/fast":
                self.wfile.write(b"x" * 65536)
                return
            if self.path == "/bulk":
                for _ in range(512):
                    self.wfile.write(b"x" * 65536)
                return
            for _ in range(64):
                self.wfile.write(b"x" * 1024)
                self.wfile.flush()
                time.sleep(0.1)

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

            def benchmark():
                trials = []
                for _ in range(3):
                    samples = []
                    for _ in range(20):
                        response = subprocess.run(
                            [
                                "curl",
                                "--silent",
                                "--fail",
                                "--max-time",
                                "5",
                                "--noproxy",
                                "",
                                "--socks5-hostname",
                                "127.0.0.1:10880",
                                "http://127.0.0.1:18080/fast",
                                "-o",
                                "/dev/null",
                                "--write-out",
                                "%{time_starttransfer} %{time_total}",
                            ],
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                        samples.append(tuple(map(float, response.stdout.split())))
                        time.sleep(0.1)
                    bulk = subprocess.run(
                        [
                            "curl",
                            "--silent",
                            "--fail",
                            "--max-time",
                            "10",
                            "--noproxy",
                            "",
                            "--socks5-hostname",
                            "127.0.0.1:10880",
                            "http://127.0.0.1:18080/bulk",
                            "-o",
                            "/dev/null",
                            "--write-out",
                            "%{size_download} %{time_total}",
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    size, seconds = map(float, bulk.stdout.split())
                    assert size == 33554432
                    trials.append(
                        {
                            "requests": len(samples),
                            "p95_established_response_ms": sorted(s[0] * 1000 for s in samples)[18],
                            "median_effective_bytes_per_second": statistics.median(
                                65536 / s[1] for s in samples
                            ),
                            "bulk_bytes": int(size),
                            "bulk_bytes_per_second": size / seconds,
                        }
                    )
                return trials

            normal_baseline = benchmark()
            observed = []
            observer_done = threading.Event()
            observer_errors = []

            def observe():
                try:
                    stream(
                        secret,
                        "ali",
                        {phone, desktop},
                        observed.append,
                        lambda: not observer_done.is_set(),
                        seconds=90,
                    )
                except Exception as exc:
                    observer_errors.append(type(exc).__name__)

            watcher = threading.Thread(target=observe, daemon=True)
            watcher.start()
            deadline = time.monotonic() + 3
            while not observed and time.monotonic() < deadline:
                time.sleep(0.1)
            assert observed, "Continuous stream did not open"
            normal_enabled = benchmark()
            p95_before = max(t["p95_established_response_ms"] for t in normal_baseline)
            p95_after = max(t["p95_established_response_ms"] for t in normal_enabled)
            throughput_before = statistics.median(
                t["bulk_bytes_per_second"] for t in normal_baseline
            )
            throughput_after = statistics.median(t["bulk_bytes_per_second"] for t in normal_enabled)
            comparison = {
                "baseline": normal_baseline,
                "enabled": normal_enabled,
                "p95_delta_ms": p95_after - p95_before,
                "throughput_ratio": throughput_after / throughput_before,
                "passed": p95_after - p95_before <= max(20, p95_before * 0.1)
                and throughput_after / throughput_before >= 0.95,
            }
            if not comparison["passed"]:
                print(json.dumps({"normal_path_comparison": comparison}), flush=True)
            assert comparison["passed"]
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
            # UDP echo target and SOCKS5 association remain in container loopback.
            udp_target = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_target.bind(("127.0.0.1", 18081))
            udp_target.settimeout(3)

            def echo_udp():
                data, peer = udp_target.recvfrom(1024)
                udp_target.sendto(data, peer)

            echo = threading.Thread(target=echo_udp, daemon=True)
            echo.start()
            with socket.create_connection(("127.0.0.1", 10880), timeout=3) as association:
                association.sendall(b"\x05\x01\x00")
                assert association.recv(2) == b"\x05\x00"
                association.sendall(b"\x05\x03\x00\x01" + b"\x00" * 6)
                header = association.recv(4)
                assert header[:2] == b"\x05\x00"
                length = 4 if header[3] == 1 else 16
                address = association.recv(length)
                relay_port = int.from_bytes(association.recv(2), "big")
                assert len(address) == length
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_client:
                    udp_client.settimeout(3)
                    payload = b"synthetic-udp-metadata"
                    packet = (
                        b"\x00\x00\x00\x01"
                        + socket.inet_aton("127.0.0.1")
                        + (18081).to_bytes(2, "big")
                        + payload
                    )
                    udp_client.sendto(packet, ("127.0.0.1", relay_port))
                    reply = udp_client.recv(1024)
                    assert reply.endswith(payload)
                    time.sleep(0.2)
            echo.join(timeout=1)
            udp_target.close()
            assert any(
                e.kind == "connection"
                and e.transport == "udp"
                and e.device_id == phone
                and e.target_port == 18081
                for e in observed
            )
            assert any(
                e.kind == "connection" and e.lifecycle == "active" and e.download_bytes > 0
                for e in observed
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                closed_events = [
                    e
                    for e in observed
                    if e.kind == "connection" and e.lifecycle == "closed" and e.target_port == 18080
                ]
                if {e.device_id for e in closed_events} == {phone, desktop}:
                    break
                time.sleep(0.05)
            assert {e.device_id for e in closed_events} == {phone, desktop}, [
                (e.kind, e.code) for e in observed
            ]
            assert all(e.target_ip == "127.0.0.1" and e.target_port == 18080 for e in closed_events)
            assert all(e.source_ip == "127.0.0.1" and e.transport == "tcp" for e in closed_events)
            assert all(e.target_host is None and e.download_bytes >= 65536 for e in closed_events)
            observer_done.set()
            watcher.join(timeout=8)
            assert not watcher.is_alive()
            print(
                json.dumps(
                    {
                        "scope": "isolated continuous native API metadata normal-path only",
                        "continuous_identity_and_destination": "passed",
                        "udp_identity_and_destination": "passed",
                        "long_connection_interval_updates": "passed",
                        "ip_only_remains_no_domain": "passed",
                        "old_usage_snapshot_still_works": "passed",
                        "events": len(observed),
                        "core": "sing-box 1.14.3",
                        "unauthenticated_rejected": True,
                        "devices": results,
                        "normal_path_comparison": comparison,
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
