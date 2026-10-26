"""Bounded read-only Linux sources and journal/audit projections.

No active requests, arbitrary commands, destination lists or raw messages leave
this module. Unknown formats remain unknown; a TLS/reset log is not a user outage.
"""

import ipaddress
import json
import os
import re
import resource
import selectors
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

from wayvia.evidence import context, number, safe_time, token
from wayvia.telemetry.records import UNITS, EvidenceError, digest, project, stamp

UNIT_IDS = {
    "7d4958e842da4a758f6c1cdc7b36dcc5": "service_starting",
    "39f53479d3a045ac8e11786248231fbf": "service_started",
    "de5b426a63be47a7b6ac3eaac82e2f6f": "service_stopping",
    "9d1aaa27d60140bd96365438aad20286": "service_stopped",
    "be02cf6855d2428ba40df7e9d022f03d": "service_start_failed",
}
MAINTENANCE = {"unattended-upgrades.service", "apt-daily-upgrade.service"}


def command(argv, *, timeout=2, max_bytes=1048576, budget=None, env=None):
    timeout = min(timeout, budget.remaining()) if budget else timeout
    if timeout <= 0:
        return "timeout", ""
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    except OSError:
        return "unavailable", ""
    output = bytearray()
    deadline = time.monotonic() + timeout
    status = "ok"
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (budget and budget.remaining() <= 0):
                    status = "timeout"
                    break
                ready = selector.select(min(0.1, remaining))
                if not ready:
                    continue
                block = os.read(process.stdout.fileno(), 16384)
                if not block:
                    break
                output.extend(block)
                if len(output) > max_bytes:
                    status = "truncated"
                    break
        if status == "ok":
            try:
                code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
                status = "ok" if code == 0 else "error"
            except subprocess.TimeoutExpired:
                status = "timeout"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
    # A truncated command is not parsed as a complete snapshot.
    return status, output.decode(errors="replace") if status == "ok" else ""


def tcp_rows(raw, *, salt):
    rows = []
    current = None
    for line in raw.splitlines():
        if line and not line[0].isspace():
            current = None
            words = line.split()
            if (
                len(words) < 5
                or words[0] != "ESTAB"
                or not words[1].isdigit()
                or not words[2].isdigit()
            ):
                continue
            rows.append(
                {
                    "id": digest([salt, words[3], words[4]])[:16],
                    "recv_q_bytes": int(words[1]),
                    "send_q_bytes": int(words[2]),
                }
            )
            current = rows[-1]
            if len(rows) > 256:
                raise ValueError("too many carrier sockets")
        elif current is not None:
            fields = {
                "rtt": "rtt_ms",
                "rto": "rto_ms",
                "cwnd": "cwnd",
                "bytes_sent": "bytes_sent",
                "bytes_received": "bytes_received",
                "bytes_retrans": "bytes_retrans",
                "snd_wnd": "peer_receive_window_bytes",
                "rcv_wnd": "receive_window_bytes",
                "rcv_space": "receive_space_bytes",
            }
            for source, target in fields.items():
                match = re.search(r"\b" + source + r":([0-9.]+)(?:/([0-9.]+))?\b", line)
                if match:
                    rows[-1][target] = float(match[1])
                    if source == "rtt" and match[2]:
                        rows[-1]["rttvar_ms"] = float(match[2])
            match = re.search(r"\bretrans:([0-9]+)(?:/([0-9]+))?\b", line)
            if match:
                rows[-1]["retrans_total"] = int(match[2] or match[1])
    return rows


def interface_row(raw, previous, elapsed):
    data = json.loads(raw)
    if not isinstance(data, list) or len(data) != 1:
        raise ValueError("invalid interface snapshot")
    link = data[0]
    stats = link.get("stats64", link.get("stats", {}))
    result = {"ifindex": number(link.get("ifindex")), "counter_state": "unknown"}
    for direction in ("rx", "tx"):
        for name in ("bytes", "packets", "errors", "dropped"):
            result[f"{direction}_{name}"] = number(stats.get(direction, {}).get(name))
        old = previous.get(f"{direction}_bytes") if previous else None
        current = result[f"{direction}_bytes"]
        rate = None
        if (
            old is not None
            and current is not None
            and elapsed is not None
            and elapsed > 0
            and previous.get("ifindex") == result["ifindex"]
        ):
            if current >= old:
                rate = (current - old) / elapsed
                result["counter_state"] = "continuous"
            else:
                result["counter_state"] = "reset"
        result[f"{direction}_bytes_per_second"] = rate
    return result


def host_metrics(previous=None, *, proc=Path("/proc")):
    fields = [int(v) for v in (proc / "stat").read_text().splitlines()[0].split()[1:9]]
    mem = {
        line.split(":")[0]: int(line.split()[1]) * 1024
        for line in (proc / "meminfo").read_text().splitlines()
        if line.split(":")[0] in {"MemAvailable", "MemTotal"}
    }
    load = [float(v) for v in (proc / "loadavg").read_text().split()[:3]]
    busy = None
    if previous and len(previous) == len(fields):
        delta = [a - b for a, b in zip(fields, previous, strict=True)]
        if all(v >= 0 for v in delta) and sum(delta) > 0:
            busy = 100 * (sum(delta) - delta[3] - delta[4]) / sum(delta)
    pressure = {}
    for name in ("cpu", "memory", "io"):
        path = proc / "pressure" / name
        if path.exists():
            pressure[name] = {
                line.split()[0]: {
                    k: float(v) for k, v in (part.split("=") for part in line.split()[1:])
                }
                for line in path.read_text().splitlines()
            }
    return {
        "cpu_busy_percent": busy,
        "memory_available_bytes": mem.get("MemAvailable"),
        "memory_total_bytes": mem.get("MemTotal"),
        "load1": load[0],
        "load5": load[1],
        "load15": load[2],
        "pressure": pressure,
    }, fields


def journal_events(row, role):
    """Parse only trusted units and fixed systemd IDs or structured Wayvia output."""
    unit = row.get("UNIT") if row.get("_PID") == "1" else row.get("_SYSTEMD_UNIT")
    if unit not in UNITS:
        return []
    micros = row.get("__REALTIME_TIMESTAMP")
    if not isinstance(micros, str) or not micros.isdigit():
        return []
    at = stamp(int(micros) / 1000000)
    base = {
        **context(role=role, observed_at=at),
        "type": "event",
        "source": "journal",
        "unit": unit,
        "boot_id": str(uuid.UUID(row["_BOOT_ID"]))
        if token(row.get("_BOOT_ID"), r"[a-f0-9]{32}")
        else None,
        "monotonic_ms": int(row["__MONOTONIC_TIMESTAMP"]) / 1000
        if str(row.get("__MONOTONIC_TIMESTAMP", "")).isdigit()
        else None,
        "software_version": None,
        "producer_digest": None,
        "event_id": digest([role, row.get("__CURSOR"), micros, unit, row.get("MESSAGE_ID")]),
    }
    if row.get("_PID") == "1" and row.get("MESSAGE_ID") in UNIT_IDS:
        kind = UNIT_IDS[row["MESSAGE_ID"]]
        if unit in MAINTENANCE:
            kind = "maintenance_completed" if kind == "service_stopped" else "maintenance_started"
        return [project({**base, "kind": kind})]
    if unit in {"ssh.service", "sshd.service"}:
        return [project({**base, "kind": "management_ssh"})]
    message = row.get("MESSAGE")
    if not isinstance(message, str) or len(message) > 262144:
        return []
    try:
        decoded = json.loads(message)
    except ValueError:
        if "wayvia-internal-probe" in message and unit in {
            "wayvia-ingress.service",
            "wayvia-exit.service",
        }:
            return [project({**base, "kind": "probe_transport"})]
        # Raw core TLS/reset text is deliberately not classified as a business failure.
        return []
    if (
        isinstance(decoded, dict)
        and decoded.get("type") == "agent_event"
        and unit in {"wayvia-agent.service", "wayvia-monitor.service", "wayvia-recover.service"}
    ):
        affected = decoded.get("unit") if decoded.get("unit") in UNITS else unit
        return [
            project(
                {
                    **decoded,
                    "type": "event",
                    "source": "agent",
                    "unit": affected,
                    "source_unit": unit,
                }
            )
        ]
    snapshots = decoded if isinstance(decoded, list) else [decoded]
    events = []
    for transaction in snapshots[:8]:
        if not isinstance(transaction, dict) or transaction.get("role") != role:
            continue
        generation = transaction.get("generation", transaction.get("id"))
        common = {**base, "generation": generation, "revision": transaction.get("revision")}
        if transaction.get("state") == "DEGRADED":
            events.append(project({**common, "kind": "health_degraded"}))
        for index, evidence in enumerate(transaction.get("evidence", [])[:7]):
            if not isinstance(evidence, dict):
                continue
            failed = evidence.get("status") != "healthy"
            events.append(
                project(
                    {
                        **common,
                        **evidence,
                        "type": "event",
                        "source": "journal",
                        "unit": unit,
                        "role": role,
                        "generation": generation,
                        "revision": transaction.get("revision"),
                        "observed_at": safe_time(evidence.get("observed_at")) or at,
                        "event_id": digest([base["event_id"], index, evidence.get("event_id")]),
                        "kind": "probe_failed" if failed else "probe_healthy",
                    }
                )
            )
    return events


def audit_events(path, *, since, until, role):
    """Read existing audit rows only. No control-plane migration or secret lookup."""
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or path.stat().st_mode & 0o077:
        raise ValueError("unsafe audit source")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT a.id,a.action,a.object_id,a.created_at,s.device_id FROM audit a "
            "LEFT JOIN subscriptions s ON a.object_id=s.id "
            "WHERE a.created_at>=? AND a.created_at<=? ORDER BY a.id LIMIT 257",
            (since, until),
        ).fetchall()
    finally:
        connection.close()
    kinds = {
        "device.add": "device_added",
        "device.rotate": "device_rotated",
        "device.revoke": "device_revoked",
        "subscription.issue": "subscription_issued",
        "subscription.revoke": "subscription_revoked",
    }
    events = []
    for ident, action, obj, at, device in rows[:256]:
        if action not in kinds:
            continue
        events.append(
            project(
                {
                    **context(role=role, observed_at=stamp(at)),
                    "software_version": None,
                    "producer_digest": None,
                    "event_id": digest([role, "audit", ident, action, obj, at]),
                    "type": "event",
                    "kind": kinds[action],
                    "device_id": obj if action.startswith("device.") else device,
                    "source": "audit",
                    "monotonic_ms": None,
                    "boot_id": None,
                    "identity_source": "credential_only",
                }
            )
        )
    return events, "truncated" if len(rows) > 256 else "ok"


class Sampler:
    def __init__(
        self, *, role, peer, port, interface, run_id, runner=command, measure_clock_offset=False
    ):
        self.measure_clock_offset = measure_clock_offset
        self.role, self.peer = role, str(ipaddress.IPv4Address(peer))
        if not 1 <= port <= 65535 or token(interface, r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,31}") is None:
            raise ValueError("invalid diagnostic scope")
        self.port, self.interface, self.run_id, self.runner = port, interface, run_id, runner
        self.previous = None
        self.cpu = None
        self.versions = {}
        self.version_time = None

    def sample(self, metadata, *, budget):
        start, cpu = time.monotonic(), time.process_time()
        row = {**context(**metadata), "type": "sample", "sources": {}, "interfaces": {}}
        row["monotonic_ms"] = start * 1000
        elapsed = None
        if self.previous and self.previous["boot_id"] == row["boot_id"] and row["boot_id"]:
            elapsed = (row["monotonic_ms"] - self.previous["monotonic_ms"]) / 1000
            if not 0 < elapsed <= 120:
                elapsed = None
        direction = "dport" if self.role == "ingress" else "sport"
        state, raw = self.runner(
            [
                "ss",
                "-Htin",
                "dst",
                self.peer,
                direction,
                "=",
                f":{self.port}",
            ],
            budget=budget,
        )
        row["sources"]["tcp"] = state
        try:
            row["tcp"] = tcp_rows(raw, salt=self.run_id) if state == "ok" else []
        except ValueError:
            row["sources"]["tcp"] = "error"
        states = []
        for key, name, ns in [
            ("physical", self.interface, None),
            ("tunnel", "tun0", "wayvia-egress" if self.role == "ingress" else None),
        ]:
            argv = ["ip"] + (["-n", ns] if ns else []) + ["-s", "-j", "link", "show", "dev", name]
            state, raw = self.runner(argv, budget=budget)
            try:
                old = self.previous["interfaces"].get(key) if self.previous and elapsed else None
                row["interfaces"][key] = interface_row(raw, old, elapsed) if state == "ok" else {}
            except (ValueError, TypeError, KeyError):
                state = "error"
            states.append(state)
        row["sources"]["interfaces"] = "ok" if all(s == "ok" for s in states) else "unknown"
        try:
            row["host"], self.cpu = host_metrics(self.cpu)
            row["sources"]["host"] = "ok"
        except (OSError, ValueError, IndexError):
            row["sources"]["host"] = "unavailable"
        # NTP synchronization is measured separately; offset remains explicitly unknown.
        state, raw = self.runner(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"], budget=budget
        )
        row["clock_sync"] = (
            "synchronized"
            if state == "ok" and raw.strip() == "yes"
            else ("unsynchronized" if state == "ok" and raw.strip() == "no" else "unknown")
        )
        row["sources"]["clock"] = state
        if self.measure_clock_offset:
            from wayvia.telemetry.clock import tracking

            state, raw = self.runner(
                ["chronyc", "-n", "tracking"],
                budget=budget,
                max_bytes=4096,
                env={**os.environ, "LC_ALL": "C"},
            )
            measured = tracking(raw, time.time()) if state == "ok" else {}
            row.update(measured)
            row["sources"]["clock"] = "ok" if measured else "unavailable"
        if self.version_time is None or start - self.version_time >= 60:
            for key, argv, pattern in [
                ("sing_box", ["sing-box", "version"], r"sing-box version ([0-9]+\.[0-9]+\.[0-9]+)"),
                ("openvpn", ["openvpn", "--version"], r"OpenVPN ([0-9]+\.[0-9]+\.[0-9]+)"),
                ("v2raya", ["v2raya", "--version"], r"([0-9]+\.[0-9]+\.[0-9]+)"),
                (
                    "v2raya_core",
                    ["v2raya_core", "version"],
                    r"V2RAYA_CORE ([0-9]+\.[0-9]+\.[0-9]+)",
                ),
            ]:
                state, raw = self.runner(argv, budget=budget, max_bytes=32768)
                match = re.match(pattern, raw) if state == "ok" else None
                self.versions[key] = match[1] if match else None
            self.version_time = start
            self.version_at = row["observed_at"]
        row["component_versions"] = self.versions.copy()
        row["versions_observed_at"] = self.version_at
        row["collector"] = {
            "duration_ms": (time.monotonic() - start) * 1000,
            "cpu_ms": (time.process_time() - cpu) * 1000,
            "rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1024 if os.uname().sysname == "Linux" else 1),
        }
        self.previous = row
        return row


def collect_journal(*, since, until, role, budget, runner=command):
    argv = [
        "journalctl",
        "--no-pager",
        "--output=json",
        "--since",
        stamp(since),
        "--until",
        stamp(until),
        "-n",
        "256",
        "--output-fields=__CURSOR,__REALTIME_TIMESTAMP,__MONOTONIC_TIMESTAMP,_BOOT_ID,_SYSTEMD_UNIT,_PID,UNIT,MESSAGE_ID,MESSAGE",
    ]
    for unit in sorted(UNITS):
        argv.extend(["-u", unit])
    status, raw = runner(argv, budget=budget)
    if status != "ok":
        return [], status
    lines = raw.splitlines()
    events = []
    try:
        for line in lines:
            events.extend(journal_events(json.loads(line), role))
    except (ValueError, TypeError, AttributeError, EvidenceError):
        return [], "error"
    return events, "truncated" if len(lines) >= 256 else "ok"
