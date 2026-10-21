"""Bounded read-only diagnostics beside the existing monitor; never changes health state.

Only root-owned observation files are written. Active requests contain no user data,
use fixed public test endpoints, and cannot authorize or block the production path.
"""

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

UTC = dt.UTC
ROOT = Path("/var/lib/wayvia/agent")
OUT = Path("/var/lib/wayvia/observations")
MAX_BYTES = 96 * 1024 * 1024
LAYERS = {"access", "carrier", "vpn", "egress", "dns", "ipv6", "return"}
STATES = {"ACTIVE", "DEGRADED", "SUPERSEDED", "ROLLED_BACK", "RECOVERY_REQUIRED"}
CODES = {
    "HTTP_DEADLINE",
    "HTTP_IDENTITY_MISMATCH",
    "HTTP_INVALID_RESPONSE",
    "HTTP_REQUEST_FAILED",
    "HTTP_TIMEOUT",
    "HTTP_TLS_FAILED",
    "HTTP_TRANSPORT_FAILED",
    "PROTOCOL_PROBE_FAILED",
    "PROBE_DEADLINE",
    "PROBE_INVALID",
    "PROBE_UNSUPPORTED",
    "PROBE_UPGRADE_REQUIRED",
}


def stamp():
    return dt.datetime.now(UTC).isoformat()


def safe_code(value):
    return value if isinstance(value, str) and value in CODES else "REDACTED"


def safe_time(value):
    try:
        parsed = dt.datetime.fromisoformat(value)
        return parsed.astimezone(UTC).isoformat() if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def number(value):
    return value if type(value) in {int, float} and 0 <= value < 10**20 else None


def sanitize_monitor(row):
    state = row.get("state")
    result = {
        "state": state if isinstance(state, str) and state in STATES else "UNKNOWN",
        "updated_at": safe_time(row.get("updated_at")),
        "evidence": [],
    }
    for item in row.get("evidence", []):
        if not isinstance(item, dict) or not isinstance(item.get("layer"), str):
            continue
        if item["layer"] not in LAYERS:
            continue
        evidence = {
            "layer": item["layer"],
            "status": "healthy" if item.get("status") == "healthy" else "unhealthy",
            "duration_ms": number(item.get("duration_ms")),
            "observed_at": safe_time(item.get("observed_at")),
        }
        if item.get("error"):
            evidence["error"] = safe_code(item["error"])
        evidence["attempts"] = []
        for a in item.get("attempts", [])[:3]:
            if not isinstance(a, dict):
                continue
            evidence["attempts"].append(
                {
                    "endpoint": number(a.get("endpoint")),
                    "duration_ms": number(a.get("duration_ms")),
                    "curl_code": number(a.get("curl_code")),
                    "http_status": number(a.get("http_status")),
                    "result": "healthy"
                    if a.get("result") == "healthy"
                    else safe_code(a.get("result")),
                    "transport_closed_after_response": a.get("transport_closed_after_response")
                    is True,
                }
            )
        result["evidence"].append(evidence)
    return result


def command(argv, timeout=3):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 125, ""


def host_metrics():
    cpu = Path("/proc/stat").read_text().splitlines()[0].split()[1:9]
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in {"MemAvailable", "MemTotal", "SwapFree", "SwapTotal"}:
            mem[key] = int(value.split()[0])
    pressure = {}
    for name in ["cpu", "memory", "io"]:
        p = Path("/proc/pressure") / name
        if p.exists():
            pressure[name] = {
                line.split()[0]: {k: float(v) for k, v in (x.split("=") for x in line.split()[1:])}
                for line in p.read_text().splitlines()
            }
    counters = {}
    wanted = {
        "RetransSegs",
        "OutSegs",
        "InSegs",
        "InErrs",
        "TCPTimeouts",
        "TCPSynRetrans",
        "TCPLostRetransmit",
        "TCPMemoryPressures",
        "ListenDrops",
    }
    for path in ["/proc/net/snmp", "/proc/net/netstat"]:
        lines = Path(path).read_text().splitlines()
        for i in range(0, len(lines) - 1, 2):
            keys, vals = lines[i].split(), lines[i + 1].split()
            for key, value in zip(keys[1:], vals[1:], strict=True):
                if key in wanted:
                    counters[keys[0] + key] = int(value)
    return {
        "cpu_jiffies": [int(v) for v in cpu],
        "load": [float(v) for v in Path("/proc/loadavg").read_text().split()[:3]],
        "memory_kib": mem,
        "pressure": pressure,
        "host_global_tcp_counters": counters,
    }


def interface_metrics(interface, namespace=None):
    args = (
        ["ip"]
        + (["-n", namespace] if namespace else [])
        + ["-s", "-j", "link", "show", "dev", interface]
    )
    code, raw = command(args)
    if code:
        return {"exit": code}
    rows = json.loads(raw)
    return [
        {"operstate": r.get("operstate"), "stats": r.get("stats64", r.get("stats", {}))}
        for r in rows
    ]


def socket_metrics(role, peer):
    filt = (
        ["dst", peer, "dport", "=", ":443"]
        if role == "ingress"
        else ["dst", peer, "sport", "=", ":443"]
    )
    code, raw = command(["ss", "-Htin", *filt])
    rows = []
    for line in raw.splitlines():
        if line and not line[0].isspace():
            rows.append(
                {
                    "id": hashlib.sha256(line.split(maxsplit=3)[-1].encode()).hexdigest()[:16],
                    "state": line.split()[0] if re.match(r"^[A-Z-]+\s", line) else "UNKNOWN",
                }
            )
        elif rows:
            for key in [
                "rtt",
                "rto",
                "cwnd",
                "retrans",
                "bytes_sent",
                "bytes_received",
                "bytes_retrans",
                "lastsnd",
                "lastrcv",
                "unacked",
            ]:
                match = re.search(r"\b" + key + r":([0-9.]+(?:/[0-9.]+)?)", line)
                if match:
                    rows[-1][key] = match[1]
    return {"exit": code, "known_carrier_connections": rows}


def peer_ping(peer):
    code, raw = command(["ping", "-n", "-c", "1", "-W", "1", peer], timeout=2)
    match = re.search(r"time[=<]([0-9.]+) ms", raw)
    return {"exit": code, "rtt_ms": float(match[1]) if match else None}


def parse_http(code, output, expected_ip=None):
    result = {"curl_exit": code, "valid_https_response": False, "single_valid_trace_ip": False}
    if expected_ip is not None:
        result["expected_exit_match"] = False
    try:
        body, metrics = output.rsplit("\nWAYVIA_METRICS:", 1)
        data = json.loads(metrics)
        for key in [
            "http_code",
            "ssl_verify_result",
            "time_connect",
            "time_appconnect",
            "time_starttransfer",
            "time_total",
            "size_download",
        ]:
            result[key] = number(data.get(key))
        addresses = [line[3:] for line in body.splitlines() if line.startswith("ip=")]
        result["single_valid_trace_ip"] = len(addresses) == 1 and bool(
            ipaddress.ip_address(addresses[0])
        )
        if expected_ip is not None:
            result["expected_exit_match"] = len(addresses) == 1 and ipaddress.ip_address(
                addresses[0]
            ) == ipaddress.ip_address(expected_ip)
        result["valid_https_response"] = (
            code == 0 and data.get("http_code") == 200 and data.get("ssl_verify_result") == 0
        )
    except (ValueError, TypeError):
        result["parse_error"] = True
    return result


def http_control(ip, namespace, expected):
    prefix = ["ip", "netns", "exec", namespace] if namespace else []
    code, output = command(
        [
            *prefix,
            "curl",
            "--disable",
            "--silent",
            "--show-error",
            "--noproxy",
            "*",
            "--proxy",
            "",
            "--proto",
            "=https",
            "--connect-timeout",
            "2",
            "--max-time",
            "4",
            "--max-filesize",
            "4096",
            "--write-out",
            "\nWAYVIA_METRICS:%{json}",
            "--",
            "https://" + ip + "/cdn-cgi/trace",
        ],
        timeout=5,
    )
    return parse_http(code, output, expected)


def dns_control(resolver, namespace):
    prefix = ["ip", "netns", "exec", namespace] if namespace else []
    code, raw = command(
        [
            *prefix,
            "dig",
            "+tries=1",
            "+timeout=2",
            "+noall",
            "+comments",
            "+stats",
            "@" + resolver,
            "example.com",
            "A",
        ],
        timeout=3,
    )
    status = re.search(r"status: ([A-Z0-9]+)", raw)
    timing = re.search(r"Query time: ([0-9]+) msec", raw)
    return {
        "exit": code,
        "rcode": status[1] if status else None,
        "query_ms": int(timing[1]) if timing else None,
    }


def control_job(label, kind, target, namespace, expected):
    started = time.monotonic()
    result = (
        http_control(target, namespace, expected)
        if kind == "http"
        else dns_control(target, namespace)
    )
    return {
        "type": "control",
        "label": label,
        "started_at": dt.datetime.fromtimestamp(
            time.time() - (time.monotonic() - started), UTC
        ).isoformat(),
        "at": stamp(),
        "duration_ms": round(1000 * (time.monotonic() - started)),
        "result": result,
    }


class SizeLimitError(RuntimeError):
    pass


class Writer:
    def __init__(self, path, limit=MAX_BYTES):
        self.file = path.open("x", encoding="utf-8")
        path.chmod(0o600)
        self.used = 0
        self.limit = limit

    def put(self, row):
        text = json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n"
        size = len(text.encode())
        if self.used + size > self.limit:
            raise SizeLimitError("size_limit")
        self.file.write(text)
        self.file.flush()
        self.used += size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=["ingress", "exit"], required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--peer-ip", required=True)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--until", required=True)
    args = parser.parse_args()
    assert os.geteuid() == 0 and re.fullmatch(r"[a-f0-9]{32}", args.generation)
    assert re.fullmatch(r"[a-zA-Z0-9-]{1,64}", args.run_id)
    assert re.fullmatch(r"[a-zA-Z0-9_.-]{1,32}", args.interface) and not args.interface.startswith(
        "-"
    )
    ipaddress.IPv4Address(args.peer_ip)
    until = dt.datetime.fromisoformat(args.until)
    assert until.tzinfo and 0 < (until - dt.datetime.now(UTC)).total_seconds() <= 86400
    deadline = time.monotonic() + (until - dt.datetime.now(UTC)).total_seconds()
    runtime = ROOT / (args.role + "-runtime.json")
    journal = ROOT / (args.generation + ".json")
    expected = next(
        p["expected"]
        for p in json.loads(journal.read_text())["plan"]["probes"]
        if p["layer"] == "egress"
    )
    ipaddress.IPv4Address(expected)
    assert json.loads(runtime.read_text()).get("transaction_id") == args.generation
    os.umask(0o077)
    directory = OUT / args.run_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    writer = Writer(directory / "samples.jsonl")
    stop = []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(True))
    signal.signal(signal.SIGINT, lambda *a: stop.append(True))
    writer.put(
        {
            "type": "start",
            "at": stamp(),
            "role": args.role,
            "generation": args.generation,
            "until": until.isoformat(),
            "sample_interval_seconds": 5,
            "control_interval_seconds": 30,
            "max_bytes": MAX_BYTES,
            "scope": "diagnostics_only_no_health_decision_or_mutation",
            "ntp": command(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])[1].strip()
            == "yes",
        }
    )
    next_controls = 0.0
    previous_monitor = None
    futures = []
    reason = "deadline"
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        try:
            while not stop and time.monotonic() < deadline:
                started = time.monotonic()
                if json.loads(runtime.read_text()).get("transaction_id") != args.generation:
                    reason = "generation_changed"
                    break
                for future in futures[:]:
                    if future.done():
                        writer.put(future.result())
                        futures.remove(future)
                row = {
                    "type": "sample",
                    "at": stamp(),
                    "monotonic": started,
                    "host": host_metrics(),
                    "physical": interface_metrics(args.interface),
                    "carrier": socket_metrics(args.role, args.peer_ip),
                    "peer_ping": peer_ping(args.peer_ip),
                }
                if args.role == "ingress":
                    row["tunnel"] = interface_metrics("tun0", "wayvia-egress")
                else:
                    row["tunnel"] = interface_metrics("tun0")
                monitor = sanitize_monitor(json.loads(journal.read_text()))
                if monitor != previous_monitor:
                    row["monitor"] = monitor
                    previous_monitor = monitor
                writer.put(row)
                if started >= next_controls and not futures:
                    spaces = [("host", None, expected if args.role == "exit" else None)]
                    if args.role == "ingress":
                        spaces.append(("vpn", "wayvia-egress", expected))
                    for label, namespace, identity in spaces:
                        for kind, target, name in [
                            ("http", "1.1.1.1", "cf_primary"),
                            ("http", "1.0.0.1", "cf_alternative"),
                            ("dns", "1.1.1.1", "dns_cf"),
                            ("dns", "8.8.8.8", "dns_google"),
                        ]:
                            futures.append(
                                pool.submit(
                                    control_job,
                                    label + "_" + name,
                                    kind,
                                    target,
                                    namespace,
                                    identity,
                                )
                            )
                    next_controls = started + 30
                time.sleep(
                    max(0, min(5 - (time.monotonic() - started), deadline - time.monotonic()))
                )
            for future in futures:
                writer.put(future.result())
            if stop:
                reason = "requested_stop"
        except SizeLimitError:
            reason = "size_limit"
        except Exception as exc:
            reason = "collector_error_" + type(exc).__name__
        finally:
            result = {"type": "end", "at": stamp(), "reason": reason, "bytes_written": writer.used}
            (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            writer.file.close()
    return 0 if reason == "deadline" else 2


if __name__ == "__main__":
    raise SystemExit(main())
