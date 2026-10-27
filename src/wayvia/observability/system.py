"""Passive fixed-unit journal and old control audit projections; no raw messages stored."""

import json
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

from wayvia.observability.models import ACTIONS, LogError, digest, event
from wayvia.telemetry.records import UNITS
from wayvia.telemetry.sources import UNIT_IDS, command, host_metrics, journal_events

ALLOWED_UNITS = (UNITS - {"unattended-upgrades.service", "apt-daily-upgrade.service"}) | {
    "wayvia-log.service",
    "wayvia-log-source.service",
    "wayvia-log-core.service",
    "wayvia-admin.service",
    "nginx.service",
}
SSH = re.compile(
    r"^(Accepted|Failed) (password|publickey) for (?:invalid user )?"
    r"([a-zA-Z0-9_.-]{1,32}) from ([0-9a-fA-F:.]+) port ([0-9]{1,5})(?: ssh2.*)?$"
)


def journal_row(row, host_id, role):
    unit = row.get("UNIT") if row.get("_PID") == "1" else row.get("_SYSTEMD_UNIT")
    if unit not in ALLOWED_UNITS:
        return None
    micros = row.get("__REALTIME_TIMESTAMP", "")
    if not isinstance(micros, str) or not micros.isdigit():
        return None
    common = {
        "observed_at": int(micros) / 1e6,
        "unit": unit,
        "event_id": digest([host_id, row.get("__CURSOR"), micros, unit]),
    }
    try:
        boot = str(uuid.UUID(row["_BOOT_ID"]))
    except (KeyError, ValueError, TypeError):
        boot = None
    common["boot_id"] = boot
    if row.get("_PID") == "1":
        kind = UNIT_IDS.get(row.get("MESSAGE_ID"))
        if kind not in {"service_started", "service_stopped", "service_start_failed"}:
            return None
        kind = "service_failed" if kind == "service_start_failed" else kind
        return event(host_id, role, "system", boot or "unknown", kind, **common)
    message = row.get("MESSAGE")
    if not isinstance(message, str) or len(message) > 8192:
        return None
    if unit in {"ssh.service", "sshd.service"}:
        match = SSH.fullmatch(message)
        if match:
            try:
                return event(
                    host_id,
                    role,
                    "system",
                    boot or "unknown",
                    "ssh_accepted" if match[1] == "Accepted" else "ssh_failed",
                    **common,
                    auth_method=match[2],
                    account=match[3],
                    source_ip=match[4],
                    source_port=int(match[5]),
                )
            except ValueError:
                return None
    # Reuse the existing strict structured-event projector, never its raw MESSAGE.
    try:
        projected = journal_events(row, role)
        for item in projected:
            if item.get("kind") in {
                "health_degraded",
                "health_healthy",
                "recovery_started",
                "recovery_completed",
                "recovery_failed",
                "protection_requested",
                "protection_applied",
                "protection_failed",
            }:
                return event(
                    host_id,
                    role,
                    "system",
                    boot or "unknown",
                    item["kind"],
                    **common,
                    revision=item.get("revision"),
                    generation=item.get("generation"),
                )
    except (ValueError, TypeError, KeyError):
        pass
    return None


def control_read(path, sql, values=()):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or path.stat().st_mode & 0o077:
        raise LogError("LOG_CONTROL_PATH")
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
    deadline = time.monotonic() + 1
    con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        con.execute("PRAGMA query_only=ON")
        return con.execute(sql, values).fetchall()
    finally:
        con.close()


def devices(path):
    return {r[0] for r in control_read(path, "SELECT id FROM devices LIMIT 10000")}


def audit(path, after, host_id, role):
    rows = control_read(
        path,
        "SELECT id,action,object_id,result,created_at FROM audit WHERE id>? ORDER BY id LIMIT 200",
        (after,),
    )
    events = []
    for ident, action, obj, result, at in rows:
        if action in ACTIONS:
            try:
                events.append(
                    event(
                        host_id,
                        role,
                        "audit",
                        "control-v4",
                        "management",
                        event_id=digest([host_id, "control-audit", ident]),
                        observed_at=at,
                        action=action,
                        object_id=obj,
                        result=result,
                        device_id=obj if action.startswith("device.") else None,
                        identity_source="credential_only"
                        if action.startswith("device.")
                        else "unknown",
                    )
                )
            except ValueError:
                pass
    return events, rows[-1][0] if rows else after


class SystemSource:
    def __init__(self, host_id, role, runner=command):
        self.host_id, self.role, self.runner = host_id, role, runner
        self.epoch = uuid.uuid4().hex
        self.cpu = None

    def make(self, kind, **fields):
        return event(self.host_id, self.role, "system", self.epoch, kind, **fields)

    def collect(self, cursor=None, *, disk_path=Path("/var/lib")):
        argv = [
            "journalctl",
            "--no-pager",
            "--output=json",
            "--lines=200",
            "--output-fields=__CURSOR,__REALTIME_TIMESTAMP,_BOOT_ID,_SYSTEMD_UNIT,_PID,UNIT,MESSAGE_ID,MESSAGE",
        ]
        if cursor:
            if not isinstance(cursor, str) or len(cursor) > 2048 or "\n" in cursor:
                raise LogError("LOG_CURSOR_INVALID")
            argv.extend(["--after-cursor", cursor])
        else:
            argv.extend(["--since", "-30 seconds"])
        for unit in sorted(ALLOWED_UNITS):
            argv.extend(["-u", unit])
        status, raw = self.runner(argv, max_bytes=1048576, timeout=2)
        events = []
        next_cursor = cursor
        if status != "ok":
            events.append(self.make("source_error", code="journal_gap"))
            next_cursor = None  # Next read has an explicit bounded fresh start.
        else:
            try:
                lines = raw.splitlines()
                if not cursor or len(lines) >= 200:
                    events.append(self.make("gap", code="journal_gap"))
                for line in lines:
                    row = json.loads(line)
                    item = journal_row(row, self.host_id, self.role)
                    if item:
                        events.append(item)
                    value = row.get("__CURSOR")
                    if isinstance(value, str) and len(value) <= 2048:
                        next_cursor = value
                events.append(self.make("heartbeat"))
            except (ValueError, TypeError, AttributeError):
                events.append(self.make("source_error", code="parse_failed"))
        try:
            values, self.cpu = host_metrics(self.cpu)
            events.append(
                self.make(
                    "host_metrics",
                    cpu_percent=values["cpu_busy_percent"],
                    memory_available=values["memory_available_bytes"],
                    disk_free=shutil.disk_usage(disk_path).free,
                )
            )
        except (ValueError, OSError, IndexError):
            events.append(self.make("source_error", code="source_unavailable"))
        return events, next_cursor
