"""Strict projections: raw logs, plans, addresses and bodies never enter the store."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wayvia.errors import WayviaError
from wayvia.evidence import (
    choice,
    number,
    safe_code,
    safe_time,
    sanitize_attempt,
    sanitize_context,
    token,
)


class EvidenceError(WayviaError):
    def __init__(self, code="EVIDENCE_INVALID"):
        super().__init__(code, "Diagnostic evidence operation failed", exit_code=5)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    interval_seconds: Annotated[int, Field(ge=1, le=60)] = 5
    buffer_seconds: Annotated[int, Field(ge=300, le=3600)] = 900
    pre_seconds: Annotated[int, Field(ge=120, le=600)] = 120
    post_seconds: Annotated[int, Field(ge=120, le=600)] = 120
    retention_seconds: Annotated[int, Field(ge=600, le=2592000)] = 604800
    max_bytes: Annotated[int, Field(ge=8388608, le=1073741824)] = 134217728
    max_incidents: Annotated[int, Field(ge=1, le=128)] = 32

    @model_validator(mode="after")
    def room_for_detection(self):
        if self.buffer_seconds < self.pre_seconds + 120:
            raise ValueError("Buffer must include detection/transfer allowance")
        return self


KINDS = {
    "probe_failed",
    "probe_healthy",
    "health_degraded",
    "health_healthy",
    "recovery_started",
    "recovery_completed",
    "recovery_failed",
    "protection_requested",
    "protection_applied",
    "protection_failed",
    "service_starting",
    "service_started",
    "service_stopping",
    "service_stopped",
    "service_failed",
    "service_start_failed",
    "maintenance_started",
    "maintenance_completed",
    "management_ssh",
    "probe_transport",
    "device_added",
    "device_rotated",
    "device_revoked",
    "subscription_issued",
    "subscription_revoked",
    "client_observation",
    "business_failure",
    "collector_gap",
    "configuration_changed",
}
UNITS = {
    "wayvia-monitor.service",
    "wayvia-agent.service",
    "wayvia-recover.service",
    "wayvia-ingress.service",
    "wayvia-exit.service",
    "wayvia-carrier.service",
    "wayvia-vpn-client.service",
    "wayvia-vpn-server.service",
    "wayvia-bridge.service",
    "wayvia-api.service",
    "wayvia-usage.service",
    "unattended-upgrades.service",
    "apt-daily-upgrade.service",
    "ssh.service",
    "sshd.service",
}
STATES = {"ok", "unknown", "timeout", "error", "truncated", "unavailable"}
SOURCES = ("tcp", "interfaces", "host", "clock", "journal", "audit", "peer")
TCP_FIELDS = (
    "recv_q_bytes",
    "send_q_bytes",
    "rtt_ms",
    "rttvar_ms",
    "rto_ms",
    "cwnd",
    "retrans_total",
    "bytes_sent",
    "bytes_received",
    "bytes_retrans",
    "peer_receive_window_bytes",
    "receive_window_bytes",
    "receive_space_bytes",
)
HOST_FIELDS = (
    "cpu_busy_percent",
    "memory_available_bytes",
    "memory_total_bytes",
    "load1",
    "load5",
    "load15",
)
IF_FIELDS = (
    "rx_bytes",
    "tx_bytes",
    "rx_packets",
    "tx_packets",
    "rx_dropped",
    "tx_dropped",
    "rx_errors",
    "tx_errors",
    "rx_bytes_per_second",
    "tx_bytes_per_second",
    "ifindex",
)
COST_FIELDS = ("duration_ms", "cpu_ms", "rss_bytes", "payload_bytes", "command_bytes")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def timestamp(value):
    safe = safe_time(value)
    if safe is None:
        raise EvidenceError("EVIDENCE_TIME_INVALID")
    return datetime.fromisoformat(safe).timestamp()


def stamp(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


def mapping(value):
    return value if isinstance(value, dict) else {}


def numeric(row, fields):
    row = row if isinstance(row, dict) else {}
    return {key: number(row.get(key)) for key in fields}


def project(row, *, client_authorized=False):
    if not isinstance(row, dict) or row.get("type") not in {"sample", "event"}:
        raise EvidenceError()
    at = safe_time(row.get("observed_at"))
    ident = token(row.get("event_id"), r"[a-f0-9]{32,64}")
    if at is None or ident is None:
        raise EvidenceError()
    clean = {
        **sanitize_context(row),
        "schema_version": 1,
        "event_id": ident,
        "observed_at": at,
        "type": row["type"],
    }
    if row["type"] == "event":
        kind = choice(row.get("kind"), KINDS)
        if kind == "unknown":
            raise EvidenceError("EVIDENCE_KIND_INVALID")
        if kind in {"client_observation", "business_failure"} and not client_authorized:
            raise EvidenceError("CLIENT_EVIDENCE_NOT_AUTHORIZED")
        attempts = row.get("attempts", [])
        clean.update(
            kind=kind,
            layer=choice(
                row.get("layer"), {"access", "carrier", "vpn", "egress", "dns", "ipv6", "return"}
            ),
            status=choice(
                row.get("status"),
                {"healthy", "failed", "active", "degraded", "succeeded", "pending"},
            ),
            error=safe_code(row["error"]) if row.get("error") else None,
            source=choice(
                row.get("source"),
                {"journal", "snapshot", "audit", "client", "collector", "agent", "user_confirmed"},
            ),
            unit=choice(row.get("unit"), UNITS),
            source_unit=choice(row.get("source_unit"), UNITS),
            device_id=token(row.get("device_id"), r"[a-f0-9]{32}"),
            gap_start=safe_time(row.get("gap_start")),
            gap_end=safe_time(row.get("gap_end")),
            identity_source=choice(
                row.get("identity_source"), {"credential_only", "user_confirmed", "device_observed"}
            ),
            client_version=token(row.get("client_version"), r"[0-9]{1,4}(?:\.[0-9]{1,4}){1,3}"),
            attempts=[sanitize_attempt(a) for a in attempts[:3] if isinstance(a, dict)]
            if isinstance(attempts, list)
            else [],
        )
    else:
        sockets = row.get("tcp", [])
        clean["tcp"] = (
            [
                {"id": token(s.get("id"), r"[a-f0-9]{16,64}"), **numeric(s, TCP_FIELDS)}
                for s in sockets[:256]
                if isinstance(s, dict)
            ]
            if isinstance(sockets, list)
            else []
        )
        interfaces = row.get("interfaces", {})
        interfaces = interfaces if isinstance(interfaces, dict) else {}
        clean["interfaces"] = {
            key: {
                **numeric(interfaces.get(key), IF_FIELDS),
                "counter_state": choice(
                    mapping(interfaces.get(key)).get("counter_state"),
                    {"continuous", "reset", "gap"},
                ),
            }
            for key in ("physical", "tunnel")
        }
        clean["host"] = numeric(row.get("host"), HOST_FIELDS)
        host = row.get("host", {})
        pressure = mapping(mapping(host).get("pressure"))
        clean["host"]["pressure"] = {
            name: {
                mode: numeric(
                    mapping(pressure.get(name)).get(mode), ("avg10", "avg60", "avg300", "total")
                )
                for mode in ("some", "full")
            }
            for name in ("cpu", "memory", "io")
        }
        clean["collector"] = numeric(row.get("collector"), COST_FIELDS)
        versions = mapping(row.get("component_versions"))
        clean["component_versions"] = {
            key: token(versions.get(key), r"[0-9]+\.[0-9]+\.[0-9]+")
            for key in ("sing_box", "openvpn", "v2raya", "v2raya_core")
        }
        clean["versions_observed_at"] = safe_time(row.get("versions_observed_at"))
        clean["component_versions_scope"] = (
            "installed_binary_query; running_process_version_unknown"
        )
        statuses = row.get("sources", {})
        clean["sources"] = (
            {key: choice(statuses.get(key), STATES) for key in SOURCES}
            if isinstance(statuses, dict)
            else dict.fromkeys(SOURCES, "unknown")
        )
    return clean
