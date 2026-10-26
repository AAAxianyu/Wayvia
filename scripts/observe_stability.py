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
import threading
import time
from pathlib import Path

from wayvia.evidence import (
    LAYERS,
    PHASES,
    TERMINATIONS,
    choice,
    number,
    producer_digest,
    safe_code,
    safe_time,
    sanitize_attempt,
    sanitize_context,
)
from wayvia.observation_run import (
    ObservationError,
    ObservationRun,
    SizeLimitError,
    contract_digest,
    request_stop,
)
from wayvia.observation_run import Writer as Writer

UTC = dt.UTC
ROOT = Path("/var/lib/wayvia/agent")
OUT = Path("/var/lib/wayvia/observations")
MAX_BYTES = 96 * 1024 * 1024
STATES = {"ACTIVE", "DEGRADED", "SUPERSEDED", "ROLLED_BACK", "RECOVERY_REQUIRED"}


def stamp():
    return dt.datetime.now(UTC).isoformat()


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
            **sanitize_context(item),
            "layer": item["layer"],
            "status": "healthy" if item.get("status") == "healthy" else "unhealthy",
            "duration_ms": number(item.get("duration_ms")),
            "valid_until": safe_time(item.get("valid_until")),
            "scope": choice(item.get("scope"), {"datapath", "component_readiness"}),
        }
        if item.get("error"):
            evidence["error"] = safe_code(item["error"])
        evidence["attempts"] = [
            sanitize_attempt(a) for a in item.get("attempts", [])[:3] if isinstance(a, dict)
        ]
        evidence["termination"] = choice(item.get("termination"), TERMINATIONS)
        evidence["failure_stage"] = choice(item.get("failure_stage"), PHASES)
        result["evidence"].append(evidence)
    return result


def command(argv, timeout=3, *, budget=None):
    if budget is not None:
        timeout = min(timeout, budget.remaining())
        if timeout <= 0:
            return 124, ""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired as exc:
        # Partial output is parsed locally; raw text is never written to evidence.
        output = exc.output or b""
        return 124, output.decode(errors="replace") if isinstance(output, bytes) else output
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


def interface_metrics(interface, namespace=None, *, budget=None):
    args = (
        ["ip"]
        + (["-n", namespace] if namespace else [])
        + ["-s", "-j", "link", "show", "dev", interface]
    )
    code, raw = command(args, budget=budget)
    if code:
        return {"exit": code}
    rows = json.loads(raw)
    return [
        {"operstate": r.get("operstate"), "stats": r.get("stats64", r.get("stats", {}))}
        for r in rows
    ]


def socket_metrics(role, peer, *, budget=None):
    filt = (
        ["dst", peer, "dport", "=", ":443"]
        if role == "ingress"
        else ["dst", peer, "sport", "=", ":443"]
    )
    code, raw = command(["ss", "-Htin", *filt], budget=budget)
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


def peer_ping(peer, *, budget=None):
    code, raw = command(["ping", "-n", "-c", "1", "-W", "1", peer], timeout=2, budget=budget)
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


def http_control(ip, namespace, expected, *, budget=None):
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
        budget=budget,
    )
    return parse_http(code, output, expected)


def dns_control(resolver, namespace, *, budget=None):
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
        budget=budget,
    )
    status = re.search(r"status: ([A-Z0-9]+)", raw)
    timing = re.search(r"Query time: ([0-9]+) msec", raw)
    return {
        "exit": code,
        "rcode": status[1] if status else None,
        "query_ms": int(timing[1]) if timing else None,
    }


def control_job(label, kind, target, namespace, expected, *, budget=None):
    started = time.monotonic()
    result = (
        http_control(target, namespace, expected, budget=budget)
        if kind == "http"
        else dns_control(target, namespace, budget=budget)
    )
    return {
        "type": "control",
        "observation_ended": budget is not None and budget.remaining() <= 0,
        "monotonic": started,
        "label": label,
        "started_at": dt.datetime.fromtimestamp(
            time.time() - (time.monotonic() - started), UTC
        ).isoformat(),
        "at": stamp(),
        "duration_ms": round(1000 * (time.monotonic() - started)),
        "result": result,
    }


def collect(args, run, journal, runtime, expected):
    wake = threading.Event()

    def interrupted(*unused):
        run.interrupted = True
        wake.set()

    previous_handlers = {
        sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)
    }
    next_controls = 0.0
    previous_monitor = None
    futures = []
    reason = "deadline"
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            try:
                while run.remaining() > 0:
                    started = time.monotonic()
                    if json.loads(runtime.read_text()).get("transaction_id") != args.generation:
                        reason = "generation_changed"
                        run.interrupted = True
                        break
                    current = json.loads(journal.read_text())
                    if current["plan"]["revision"] != run.contract["revision"]:
                        reason = "configuration_changed"
                        run.interrupted = True
                        break
                    for future in futures[:]:
                        if future.done():
                            run.writer.put(future.result())
                            futures.remove(future)
                    row = {
                        "type": "sample",
                        "at": stamp(),
                        "monotonic": started,
                        "host": host_metrics(),
                        "physical": interface_metrics(args.interface, budget=run),
                        "carrier": socket_metrics(args.role, args.peer_ip, budget=run),
                        "peer_ping": peer_ping(args.peer_ip, budget=run),
                        "tunnel": interface_metrics(
                            "tun0", "wayvia-egress" if args.role == "ingress" else None, budget=run
                        ),
                    }
                    monitor = sanitize_monitor(current)
                    if monitor != previous_monitor:
                        row["monitor"] = monitor
                        previous_monitor = monitor
                    run.writer.put(row)
                    if run.remaining() > 0 and started >= next_controls and not futures:
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
                                        budget=run,
                                    )
                                )
                        next_controls = started + 30
                    wake.wait(max(0, min(5 - (time.monotonic() - started), run.remaining())))
                if run.interrupted and reason == "deadline":
                    reason = "interrupted"
                # Cancel jobs that have not begun; running commands use the same deadline.
                for future in futures:
                    if not future.cancel():
                        run.writer.put(future.result())
            except SizeLimitError:
                reason = "size_limit"
                run.interrupted = True
            except Exception:
                reason = "collector_error"
                run.interrupted = True
            finally:
                for future in futures:
                    future.cancel()
        reason = run.finish(reason)
    finally:
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)
    return 0 if reason in {"deadline", "user_stop"} else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=["ingress", "exit"])
    parser.add_argument("--generation")
    parser.add_argument("--peer-ip")
    parser.add_argument("--interface")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--until")
    parser.add_argument(
        "--resume-interrupted",
        action="store_true",
        help="Allow verified continuation within the original deadline",
    )
    parser.add_argument(
        "--stop", action="store_true", help="Persist user stop; never resumes this run"
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ObservationError("OBSERVER_REQUIRES_ROOT")
    if args.stop:
        print(json.dumps(request_stop(OUT, args.run_id)))
        return 0
    if (
        args.role is None
        or not re.fullmatch(r"[a-f0-9]{32}", args.generation or "")
        or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,31}", args.interface or "")
        or safe_time(args.until) is None
    ):
        raise ObservationError("INVALID_OBSERVATION_ARGUMENTS")
    ipaddress.IPv4Address(args.peer_ip)
    runtime = ROOT / (args.role + "-runtime.json")
    journal = ROOT / (args.generation + ".json")
    current = json.loads(journal.read_text())
    expected = next(p["expected"] for p in current["plan"]["probes"] if p["layer"] == "egress")
    ipaddress.IPv4Address(expected)
    if json.loads(runtime.read_text()).get("transaction_id") != args.generation:
        raise ObservationError("OBSERVATION_GENERATION_CHANGED")
    contract = {
        "role": args.role,
        "generation": args.generation,
        "revision": current["plan"]["revision"],
        "until": safe_time(args.until),
        "max_bytes": MAX_BYTES,
        "sample_interval_seconds": 5,
        "control_interval_seconds": 30,
        "inputs_digest": contract_digest([args.peer_ip, args.interface, expected]),
        "observer_digest": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "producer_digest": producer_digest(),
    }
    os.umask(0o077)
    with ObservationRun(OUT, args.run_id, contract, resume=args.resume_interrupted) as run:
        return collect(args, run, journal, runtime, expected)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ObservationError, OSError, ValueError, KeyError, StopIteration) as exc:
        print(
            json.dumps(
                {"error": str(exc) if isinstance(exc, ObservationError) else "OBSERVER_UNAVAILABLE"}
            )
        )
        raise SystemExit(2) from None
