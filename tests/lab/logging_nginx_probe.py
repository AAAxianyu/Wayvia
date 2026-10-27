"""Synthetic nginx logging and request-cost comparison in a disposable network namespace."""

import hashlib
import importlib.util
import json
import os
import socket
import statistics
import sys
import threading
import time
from pathlib import Path

from wayvia.observability.http import nginx_event


def main():
    if not Path("/.dockerenv").exists():
        raise RuntimeError("Disposable Docker container required")
    spec = importlib.util.spec_from_file_location(
        "limits", "/repo/tests/lab/subscription_limit_probe.py"
    )
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    packets = []
    stop = threading.Event()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    path = Path("/logs/events.sock")
    path.unlink(missing_ok=True)
    sock.bind(str(path))
    os.chmod(path, 0o666)
    sock.settimeout(0.1)

    def reader():
        while not stop.is_set():
            try:
                packets.append(sock.recv(8192))
            except TimeoutError:
                pass

    worker = threading.Thread(target=reader, daemon=True)
    worker.start()
    # Same 5r/m+burst5 and global concurrency2 acceptance as the deployed baseline.
    probe.main()
    # Restart only the disposable backend after the baseline closes its socket.
    # server_close in the patched fixture releases it before this benchmark.
    server = probe.ThreadingHTTPServer(("127.0.0.1", 8787), probe.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    measurements = []
    try:
        for trial in range(3):
            durations = []
            for index in range(10):
                begin = time.monotonic()
                status, body, _ = probe.request(
                    f"/subscriptions/{probe.TOKEN}/mihomo", f"127.8.{trial + 1}.{index + 1}"
                )
                durations.append((time.monotonic() - begin) * 1000)
                assert status == 200 and body == probe.PROFILE
            measurements.append(
                {
                    "requests": len(durations),
                    "p95_ms": sorted(durations)[-1],
                    "median_ms": statistics.median(durations),
                    "throughput_bytes_per_second": len(probe.PROFILE)
                    / (statistics.median(durations) / 1000),
                }
            )
        for route, status in [
            ("/subscriptions/SECRET_EXTRA/mihomo/extra", 401),
            ("/admin/api/overview", 404),
        ]:
            assert probe.request(route, "127.9.0.1")[0] == status
        time.sleep(0.2)
        if sys.argv[-1] == "enabled":
            assert packets
            rows = [nginx_event(packet, "ali", "ingress") for packet in packets]
            assert any(r.limit_rate == "REJECTED" and r.status == 429 for r in rows)
            assert any(r.limit_concurrent == "REJECTED" and r.status == 429 for r in rows)
            assert any(r.source_ip == "127.0.0.3" for r in rows)
            assert not any(r.source_ip and r.source_ip.startswith("192.0.2.") for r in rows)
            assert len({r.request_id for r in rows}) == len(rows)
            raw = b"".join(packets)
            for marker in [
                probe.TOKEN.encode(),
                b"B" * 64,
                b"SECRET_EXTRA",
                b"/subscriptions/",
                b"hold=1",
            ]:
                assert marker not in raw
        else:
            assert not packets
        print(
            json.dumps(
                {
                    "mode": sys.argv[-1],
                    "nginx_log_events": len(packets),
                    "normal_trials": measurements,
                    "profile_sha256": hashlib.sha256(probe.PROFILE).hexdigest(),
                    "safe_fields_and_limit_reasons": "passed" if packets else "baseline",
                }
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        stop.set()
        worker.join(1)
        sock.close()
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
