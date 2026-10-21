"""Bounded, read-only normal-path comparison on the two authorized Linux hosts.

No configuration writes, credentials, packet capture, service actions, or response bodies.
JSONL is streamed over the existing SSH channel; this is not the 24-hour observer.
"""

import concurrent.futures
import datetime
import errno
import json
import pathlib
import re
import socket
import subprocess
import sys
import time


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def command(args, timeout=3):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 125, ""


def probe(label, kind, address, port):
    started = now()
    monotonic = time.monotonic()
    if kind == "icmp":
        code, out = command(["ping", "-n", "-c", "1", "-W", "1", address], 2)
        match = re.search(r"time[=<]([0-9.]+)\s*ms", out)
        result = {"exit": code, "rtt_ms": float(match[1]) if match else None}
    else:
        try:
            with socket.create_connection((address, port), timeout=1.5):
                pass
            result = {"status": "connected"}
        except (TimeoutError, socket.timeout):
            result = {"status": "timeout"}
        except OSError as exc:
            result = {"status": errno.errorcode.get(exc.errno, "OS_ERROR")}
    return {
        "label": label,
        "kind": kind,
        "started_at": started,
        "finished_at": now(),
        "duration_ms": round(1000 * (time.monotonic() - monotonic), 3),
        **result,
    }


def counters(device):
    cpu = [int(x) for x in pathlib.Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    memory = {
        line.split()[0].rstrip(":"): int(line.split()[1])
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines()
        if line.startswith(("MemAvailable:", "MemTotal:"))
    }
    nic = {}
    for field in ["rx_bytes", "tx_bytes", "rx_errors", "tx_errors", "rx_dropped", "tx_dropped"]:
        nic[field] = int(pathlib.Path(f"/sys/class/net/{device}/statistics/{field}").read_text())
    soft = [line.split() for line in pathlib.Path("/proc/net/softnet_stat").read_text().splitlines()]
    return {
        "cpu_jiffies": cpu,
        "memory_kib": memory,
        "nic": nic,
        "softnet_dropped": sum(int(x[1], 16) for x in soft),
        "softnet_time_squeeze": sum(int(x[2], 16) for x in soft),
    }


def main():
    host, cycles_text = sys.argv[1:]
    assert host in {"ali", "jp"}
    cycles = int(cycles_text)
    assert 1 <= cycles <= 120
    code, raw = command(["ip", "-j", "-4", "route", "show", "default"])
    routes = json.loads(raw)
    assert code == 0 and len(routes) == 1
    gateway, device = routes[0]["gateway"], routes[0]["dev"]
    peer = "206.119.182.131" if host == "ali" else "8.136.0.78"
    own_port = 8443 if host == "ali" else 443
    peer_port = 443 if host == "ali" else 8443
    targets = [
        ("gateway", "icmp", gateway, None),
        ("peer", "icmp", peer, None),
        ("cloudflare", "icmp", "1.1.1.1", None),
        ("google_dns", "icmp", "8.8.8.8", None),
        ("local_proxy_listener", "tcp", "127.0.0.1", own_port),
        ("peer_ssh", "tcp", peer, 22),
        ("peer_proxy_listener", "tcp", peer, peer_port),
    ]
    print(json.dumps({"type": "start", "host": host, "at": now(), "cycles": cycles,
                      "interval_seconds": 5, "device": device, "gateway": gateway,
                      "targets": [{"label": t[0], "kind": t[1], "port": t[3]} for t in targets],
                      "limit": "TCP connect only, not authenticated proxy acceptance; ICMP may be filtered."}), flush=True)
    start = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
        for index in range(cycles):
            scheduled = start + index * 5
            time.sleep(max(0, scheduled - time.monotonic()))
            at = now()
            before = counters(device)
            results = list(pool.map(lambda t: probe(*t), targets))
            print(json.dumps({"type": "sample", "at": at, "index": index,
                              "host": before, "probes": results}), flush=True)
    print(json.dumps({"type": "end", "at": now(), "reason": "cycles_complete",
                      "samples": cycles, "elapsed_seconds": round(time.monotonic() - start, 3)}), flush=True)


if __name__ == "__main__":
    main()
