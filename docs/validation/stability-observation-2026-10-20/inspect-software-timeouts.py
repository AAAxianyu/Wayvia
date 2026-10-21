"""Read-only classification of installed timeouts and incident logs; no raw logs."""

import datetime
import hashlib
import json
import pathlib
import re
import subprocess
import sys

role = sys.argv[1]
assert role in {"ingress", "exit"}
root = pathlib.Path("/etc/wayvia")
selected = ("ingress.json", "bridge.json") if role == "ingress" else ("exit.json",)
configs = {}
for name in selected:
    data = json.loads((root / name).read_text())
    configs[name] = [
        {
            key: item[key]
            for key in ("type", "tag", "connect_timeout", "bind_interface")
            if key in item
        }
        for item in data.get("outbounds", [])
    ]
events = []
error_counts = {}
units = (
    [
        "wayvia-ingress.service",
        "wayvia-bridge.service",
        "wayvia-carrier.service",
        "wayvia-vpn-client.service",
    ]
    if role == "ingress"
    else ["wayvia-exit.service", "wayvia-vpn-server.service"]
)
for start, end in [("03:49:00", "04:01:00"), ("08:24:00", "08:31:00"), ("09:04:00", "09:12:00")]:
    args = [
        "journalctl",
        "--no-pager",
        "-o",
        "json",
        "--since",
        "2026-10-21 " + start + " UTC",
        "--until",
        "2026-10-21 " + end + " UTC",
    ]
    for unit in units:
        args.extend(["-u", unit])
    for line in subprocess.check_output(args, text=True).splitlines():
        row = json.loads(line)
        message = row.get("MESSAGE", "")
        if not isinstance(message, str):
            continue
        unit = row.get("UNIT") or row.get("_SYSTEMD_UNIT")
        if unit not in units:
            continue
        # Only lifecycle records or errors for the configured public probe targets.
        lifecycle = re.match(r"^(Starting|Started|Stopping|Stopped) ", message)
        known_probe = bool(re.search(r"\b1\.(?:1\.1\.1|0\.0\.1):443\b", message))
        labels = [
            name
            for name, pattern in {
                "dial_tcp": r"dial tcp",
                "io_timeout": r"i/o timeout",
                "context_deadline": r"context deadline exceeded",
                "connection_reset": r"connection reset",
                "socks_failure": r"SOCKS|socks",
                "tls_handshake": r"TLS handshake|tls handshake",
                "certificate": r"certificate|x509",
                "closed": r"closed|EOF",
            }.items()
            if re.search(pattern, message)
        ]
        if labels:
            key = unit + ":" + ",".join(labels)
            error_counts[key] = error_counts.get(key, 0) + 1
        if not lifecycle and not known_probe:
            continue
        if not lifecycle and not labels:
            continue
        durations = re.findall(r"\b(\d+(?:\.\d+)?(?:ms|s))\]", message)
        events.append(
            {
                "at_utc": datetime.datetime.fromtimestamp(
                    int(row["__REALTIME_TIMESTAMP"]) / 1e6, datetime.timezone.utc
                ).isoformat(),
                "unit": unit,
                "event": lifecycle.group(1).lower() if lifecycle else "known_probe_path_error",
                "categories": labels,
                "duration_label": durations[-1] if durations else None,
            }
        )
modules = {}
for name in ("http_probe.py", "probe_runtime.py", "transactions.py"):
    paths = list(
        pathlib.Path("/opt/wayvia/.venv/lib").glob("python*/site-packages/wayvia/agent/" + name)
    )
    modules[name] = hashlib.sha256(paths[0].read_bytes()).hexdigest() if len(paths) == 1 else None
print(
    json.dumps(
        {
            "observed_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "role": role,
            "outbound_timeout_fields": configs,
            "installed_module_sha256": modules,
            "events": events,
            "error_category_counts_no_targets": error_counts,
        },
        indent=2,
    )
)
