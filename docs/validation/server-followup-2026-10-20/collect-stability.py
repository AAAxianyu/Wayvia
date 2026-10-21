"""Read-only, allowlisted stability summary; never emit plans, credentials or raw messages."""

import collections
import datetime
import json
import pathlib
import re
import statistics
import subprocess
import sys

role = sys.argv[1]
assert role in {"ingress", "exit"}
since = sys.argv[2] if len(sys.argv) > 2 else "2026-10-20T10:06:11.981889+00:00"
until = sys.argv[3] if len(sys.argv) > 3 else None
period = ["--since", since] + (["--until", until] if until else [])
root = pathlib.Path("/var/lib/wayvia/agent")
runtime = json.loads((root / (role + "-runtime.json")).read_text())
generation = runtime["transaction_id"]
current = json.loads((root / (generation + ".json")).read_text())
raw = subprocess.check_output(
    ["journalctl", "-u", "wayvia-monitor.service", *period, "--no-pager", "-o", "cat"], text=True
)
monitors = []
for line in raw.splitlines():
    if line.startswith('[{"id":'):
        for row in json.loads(line):
            if row.get("id") == generation:
                monitors.append(row)
by_hour = {}
durations = collections.defaultdict(list)
failed = []
fallbacks = []
transitions = []
completed_closes = []
for row in monitors:
    stamp = row["updated_at"]
    hour = stamp[:13] + ":00:00Z"
    counts = by_hour.setdefault(hour, collections.Counter())
    counts[row["state"]] += 1
    if not transitions or transitions[-1]["state"] != row["state"]:
        transitions.append({"at": stamp, "state": row["state"]})
    for e in row.get("evidence", []):
        if "duration_ms" in e:
            durations[e["layer"]].append(e["duration_ms"])
        safe = {
            k: e[k]
            for k in ["layer", "kind", "status", "observed_at", "duration_ms", "error", "attempts"]
            if k in e
        }
        if e.get("status") != "healthy":
            failed.append(safe)
        if any(a.get("transport_closed_after_response") for a in e.get("attempts", [])):
            completed_closes.append(safe)
        if any(a.get("endpoint", 0) > 0 for a in e.get("attempts", [])):
            fallbacks.append(safe)


def metrics(values):
    ordered = sorted(values)
    return {
        "samples": len(values),
        "median_ms": statistics.median(values),
        "p95_ms": ordered[min(len(values) - 1, int(len(values) * 0.95))],
        "max_ms": max(values),
    }


units = ["wayvia-agent", "wayvia-api", "wayvia-monitor.timer"] + (
    ["wayvia-ingress", "wayvia-bridge", "wayvia-carrier", "wayvia-vpn-client"]
    if role == "ingress"
    else ["wayvia-exit", "wayvia-vpn-server"]
)
unit_state = {
    u: subprocess.run(["systemctl", "is-active", u], capture_output=True, text=True).stdout.strip()
    for u in units
}
args = ["journalctl", *period, "--no-pager", "-o", "json"]
for unit in units:
    args += ["-u", unit]
logs = subprocess.check_output(args, text=True)
patterns = {
    "tls_error": r"TLS Error|TLS key negotiation failed",
    "connection_reset": r"Connection reset|connection reset",
    "inactivity": r"Inactivity timeout|SIGUSR1\[soft,ping-restart\]",
    "process_restart": r"process restarting|Process restarting|Scheduled restart job",
    "service_started": r"^Started ",
    "service_stopped": r"^Stopped ",
    "failed_unit": r"Failed with result|Failed to start",
    "remote_close": r"remote address|connection closed|Connection closed",
    "vpn_established": r"Initialization Sequence Completed",
}
events = []
for line in logs.splitlines():
    entry = json.loads(line)
    message = entry.get("MESSAGE", "")
    if not isinstance(message, str):
        continue
    for category, pattern in patterns.items():
        if re.search(pattern, message):
            ts = datetime.datetime.fromtimestamp(
                int(entry["__REALTIME_TIMESTAMP"]) / 1e6, datetime.timezone.utc
            ).isoformat()
            events.append(
                {
                    "at": ts,
                    "unit": entry.get("_SYSTEMD_UNIT") or entry.get("UNIT"),
                    "event": category,
                }
            )
            break
result = {
    "observed_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "since_utc": since,
    "role": role,
    "generation": generation,
    "current_state": current["state"],
    "first_monitor": monitors[0]["updated_at"] if monitors else None,
    "last_monitor": monitors[-1]["updated_at"] if monitors else None,
    "monitor_count": len(monitors),
    "state_counts": dict(collections.Counter(m["state"] for m in monitors)),
    "hourly_counts": by_hour,
    "layer_timings": {k: metrics(v) for k, v in durations.items()},
    "failed_checks": failed,
    "fallbacks": fallbacks,
    "state_transitions": transitions,
    "units": unit_state,
    "unit_events": events,
    "kernel_oom_kill_count_since": len(
        re.findall(
            r"oom-kill|Out of memory: Killed process",
            subprocess.run(
                ["journalctl", "-k", *period, "--no-pager", "-o", "cat"],
                capture_output=True,
                text=True,
            ).stdout,
        )
    ),
    "uptime_seconds": float(pathlib.Path("/proc/uptime").read_text().split()[0]),
}
result["completed_response_closes"] = completed_closes
result["requested_until_utc"] = until
result["unit_event_counts"] = dict(collections.Counter(e["event"] for e in events))
result["repetitive_vpn_server_event_examples"] = [
    e
    for e in events
    if e["event"] in {"tls_error", "connection_reset"} and e["unit"] == "wayvia-vpn-server.service"
][:8]
result["unit_events"] = [
    e
    for e in events
    if not (
        e["event"] in {"tls_error", "connection_reset"} and e["unit"] == "wayvia-vpn-server.service"
    )
]
print(json.dumps(result, indent=2))
