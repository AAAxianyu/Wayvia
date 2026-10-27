"""Real Linux peer credentials, bounded collector and query pipeline with synthetic events."""

import json
import os
import resource
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from wayvia.observability.http import send
from wayvia.observability.models import LogError, event
from wayvia.observability.runtime import collect
from wayvia.observability.store import LogStore
from wayvia.observability.wire import BrokerClient


def main():
    if not Path("/.dockerenv").exists():
        raise RuntimeError("Disposable Linux container required")
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        base.chmod(0o755)
        root = base / "logs"
        store = LogStore(root, create=True)
        store.update_policy({"desired_enabled": True, "min_free_bytes": 0}, 1)
        stop = threading.Event()
        sock, broker = base / "events.sock", base / "query.sock"
        worker = threading.Thread(
            target=collect,
            args=(
                root,
                sock,
                broker,
                "ali",
                "ingress",
                {os.getuid(): {"connections", "api"}},
                os.getuid(),
            ),
            kwargs={"stop": stop},
            daemon=True,
        )
        worker.start()
        while not broker.exists():
            time.sleep(0.02)
        client = BrokerClient(broker)
        assert client.policy()["desired_enabled"]
        # A different OS user cannot query even though it can reach the socket.
        result = subprocess.run(
            [sys.executable, __file__, "--unprivileged-peer", str(broker)],
            user=65534,
            group=65534,
            timeout=5,
            check=False,
        )
        assert result.returncode == 0
        before = time.process_time()
        start = time.monotonic()
        devices = [f"{i:032x}" for i in range(1, 6)]
        try:
            for i in range(100):
                item = event(
                    "ali",
                    "ingress",
                    "connections",
                    "runtime",
                    "connection",
                    device_id=devices[i % 5],
                    identity_source="credential_only",
                    connection_id=str(i),
                    core_epoch="epoch",
                    lifecycle="active",
                    source_ip="192.0.2.1",
                    target_ip="198.51.100.2",
                    target_port=443,
                    upload_bytes=100,
                    download_bytes=200,
                )
                assert send(sock, item)
                if i % 10 == 0:
                    client.query({"kind": "connection"}, limit=100)
                time.sleep(0.1)
            deadline = time.monotonic() + 5
            while client.query({"kind": "connection"})["total"] != 100:
                assert time.monotonic() < deadline
                time.sleep(0.1)
            latency = time.monotonic() - start - 10
            result = client.query({"kind": "connection", "device_id": devices[0]})
            assert result["total"] == 20
            assert all(row["device_id"] == devices[0] for row in result["items"])
            assert not list(root.glob("*-wal"))
            # An explicit stop is persistent and does not modify any control data.
            current = client.policy()
            client.update_policy({"desired_enabled": False}, current["revision"])
            assert not LogStore(root, create=True).policy()["desired_enabled"]
            print(
                json.dumps(
                    {
                        "scope": "disposable_linux_runtime",
                        "peer_auth": "passed",
                        "devices": 5,
                        "connections": 100,
                        "new_connections_per_second": 10,
                        "local_visibility_extra_seconds": round(latency, 3),
                        "cpu_seconds": round(time.process_time() - before, 3),
                        "elapsed_seconds": round(time.monotonic() - start, 3),
                        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                        "database_bytes": store.path.stat().st_size,
                        "disabled_persisted": True,
                    }
                )
            )
        finally:
            stop.set()
            worker.join(5)
            assert not worker.is_alive()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--unprivileged-peer":
        try:
            BrokerClient(sys.argv[2]).sources()
        except LogError:
            sys.exit(0)
        sys.exit(1)
    main()
