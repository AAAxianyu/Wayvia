"""Closed log contracts. Never accept raw messages, paths, headers or bodies."""

import hashlib
import ipaddress
import json
import re
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from wayvia.errors import WayviaError

DAY = 86400
MAX_EVENT = 4096
Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.:-]{1,128}$")]
Device = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Number = Annotated[float, Field(ge=0, le=10**12, allow_inf_nan=False)]
Counter = Annotated[int, Field(ge=0, le=2**63 - 1)]
SOURCES = ("nginx", "api", "connections", "system", "audit", "transfer")
KINDS = (
    "request",
    "connection",
    "heartbeat",
    "gap",
    "service_started",
    "service_stopped",
    "service_failed",
    "ssh_accepted",
    "ssh_failed",
    "host_metrics",
    "management",
    "health_degraded",
    "health_healthy",
    "recovery_started",
    "recovery_completed",
    "recovery_failed",
    "protection_requested",
    "protection_applied",
    "protection_failed",
    "source_error",
    "source_recovered",
)
CODES = (
    "none",
    "unknown",
    "rate_limit",
    "concurrency_limit",
    "unknown_429",
    "denied",
    "upstream_error",
    "client_closed",
    "reconnected",
    "core_restarted",
    "buffer_full",
    "counter_reset",
    "missing_start",
    "missing_final",
    "invalid_event",
    "journal_gap",
    "source_unavailable",
    "storage_full",
    "queue_full",
    "policy_stopped",
    "parse_failed",
    "batch_expired",
    "sequence_gap",
    "clock_skew",
    "request_failed",
)
ACTIONS = (
    "device.add",
    "device.rotate",
    "device.revoke",
    "subscription.issue",
    "subscription.revoke",
    "configuration.publish",
    "session.login",
    "session.logout",
    "policy.update",
    "export.create",
    "export.download",
    "export.delete",
    "alert.ack",
)


class LogError(WayviaError):
    def __init__(self, code="LOG_INVALID", *, status=422):
        self.status = status
        super().__init__(code, "Log operation could not be completed", exit_code=5)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def domain(value):
    if value is None:
        return None
    value = value.rstrip(".").encode("idna").decode("ascii").lower()
    if len(value) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in value.split(".")
    ):
        raise ValueError("Invalid domain")
    return value


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    log_schema_version: Literal[1] = 1
    event_id: Identifier
    host_id: Identifier
    role: Literal["ingress", "exit"]
    source: Literal["nginx", "api", "connections", "system", "audit", "transfer"]
    source_epoch: Identifier
    sequence: Counter = 0
    observed_at: Number
    kind: str
    code: str = "none"
    severity: Literal["info", "warning", "error"] = "info"
    device_id: Device | None = None
    subscription_id: Device | None = None
    identity_source: Literal["credential_only", "unknown"] = "unknown"
    request_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None
    route: (
        Literal[
            "subscription_download",
            "subscription_info",
            "health",
            "management_denied",
            "unknown_path",
        ]
        | None
    ) = None
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "OTHER"] | None = (
        None
    )
    format: Literal["mihomo", "sing-box", "unknown"] | None = None
    status: Annotated[int, Field(ge=100, le=599)] | None = None
    source_ip: str | None = None
    source_port: Annotated[int, Field(ge=1, le=65535)] | None = None
    target_ip: str | None = None
    target_host: str | None = None
    target_port: Annotated[int, Field(ge=1, le=65535)] | None = None
    domain_source: Literal["protocol", "core", "unknown"] = "unknown"
    transport: Literal["tcp", "udp", "unknown"] | None = None
    application_protocol: Literal["http", "tls", "quic", "dns", "unknown"] | None = None
    bytes_sent: Counter | None = None
    duration_ms: Number | None = None
    upstream_ms: Number | None = None
    limit_rate: Literal[
        "PASSED", "DELAYED", "REJECTED", "DELAYED_DRY_RUN", "REJECTED_DRY_RUN", "NA"
    ] = "NA"
    limit_concurrent: Literal["PASSED", "REJECTED", "REJECTED_DRY_RUN", "NA"] = "NA"
    connection_id: Identifier | None = None
    core_epoch: Identifier | None = None
    created_at: Number | None = None
    closed_at: Number | None = None
    lifecycle: Literal["active", "closed", "unknown"] | None = None
    upload_bytes: Counter | None = None
    download_bytes: Counter | None = None
    unit: (
        Annotated[str, Field(pattern=r"^(wayvia-[a-z-]+|ssh|sshd|nginx)\.(service|timer)$")] | None
    ) = None
    account: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.-]{1,32}$")] | None = None
    auth_method: Literal["password", "publickey", "unknown"] | None = None
    action: str | None = None
    object_id: Identifier | None = None
    result: (
        Literal[
            "submitted",
            "pending",
            "active",
            "applied",
            "revoked",
            "issued",
            "published",
            "failed",
            "unknown",
            "degraded",
            "success",
        ]
        | None
    ) = None
    cpu_percent: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] | None = None
    memory_available: Counter | None = None
    disk_free: Counter | None = None
    gap_start: Number | None = None
    lost_count: Counter | None = None
    revision: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    generation: Device | None = None
    boot_id: Annotated[str, Field(pattern=r"^[a-f0-9-]{36}$")] | None = None
    clock_sync: Literal["unknown", "synchronized", "unsynchronized"] = "unknown"

    @field_validator("kind")
    @classmethod
    def known_kind(cls, value):
        if value not in KINDS:
            raise ValueError("Unsupported kind")
        return value

    @field_validator("code")
    @classmethod
    def known_code(cls, value):
        if value not in CODES:
            raise ValueError("Unsupported code")
        return value

    @field_validator("action")
    @classmethod
    def known_action(cls, value):
        if value is not None and value not in ACTIONS:
            raise ValueError("Unsupported action")
        return value

    @field_validator("source_ip", "target_ip")
    @classmethod
    def ip(cls, value):
        if value is not None and "%" in value:
            raise ValueError("Scoped addresses are not supported")
        return str(ipaddress.ip_address(value)) if value is not None else None

    @field_validator("target_host")
    @classmethod
    def host(cls, value):
        return domain(value)

    @model_validator(mode="after")
    def complete(self):
        if self.kind == "request" and not (self.request_id and self.route and self.status):
            raise ValueError("Request metadata missing")
        if self.kind == "connection" and not (
            self.connection_id and self.core_epoch and self.device_id and self.lifecycle
        ):
            raise ValueError("Connection attribution missing")
        if self.device_id and self.identity_source != "credential_only":
            raise ValueError("Attribution source missing")
        if len(self.model_dump_json().encode()) > MAX_EVENT:
            raise ValueError("Event too large")
        return self

    def data(self):
        return self.model_dump(exclude_none=True)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    revision: Annotated[int, Field(ge=1)] = 1
    desired_enabled: bool = False
    hosts: dict[Identifier, Literal["ingress", "exit"]] = {"ali": "ingress", "jp": "exit"}
    connection_days: Annotated[int, Field(ge=1, le=30)] = 7
    request_days: Annotated[int, Field(ge=1, le=30)] = 14
    system_days: Annotated[int, Field(ge=1, le=90)] = 30
    audit_days: Annotated[int, Field(ge=1, le=365)] = 90
    max_data_bytes: Annotated[int, Field(ge=1048576, le=268435456)] = 268435456
    min_free_bytes: Annotated[int, Field(ge=0)] = 2147483648

    @field_validator("hosts")
    @classmethod
    def hosts_bound(cls, value):
        if not 1 <= len(value) <= 8:
            raise ValueError("One to eight hosts required")
        return value

    def retention(self, kind):
        days = (
            self.connection_days
            if kind == "connection"
            else self.request_days
            if kind in {"request", "ssh_accepted", "ssh_failed"}
            else self.audit_days
            if kind == "management"
            else self.system_days
        )
        return days * DAY


def event(host_id, role, source, epoch, kind, **values):
    at = values.pop("observed_at", time.time())
    ident = values.pop("event_id", digest([host_id, source, epoch, kind, at, values]))
    return Event(
        event_id=ident,
        host_id=host_id,
        role=role,
        source=source,
        source_epoch=epoch,
        observed_at=at,
        kind=kind,
        **values,
    )


class QueryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    since: Number | None = None
    until: Number | None = None
    kind: str | None = None
    category: Literal["system"] | None = None
    device_id: Device | None = None
    role: Literal["ingress", "exit"] | None = None
    host_id: Identifier | None = None
    source_ip: Annotated[str, Field(max_length=64)] | None = None
    target: Annotated[str, Field(max_length=253)] | None = None
    status: Annotated[int, Field(ge=100, le=599)] | None = None
    code: str | None = None

    @field_validator("kind", "code")
    @classmethod
    def known(cls, value, info):
        if value is not None and value not in (KINDS if info.field_name == "kind" else CODES):
            raise ValueError("Unknown query category")
        return value

    @field_validator("source_ip")
    @classmethod
    def network(cls, value):
        return str(ipaddress.ip_network(value, strict=False)) if value else value

    @field_validator("target")
    @classmethod
    def destination(cls, value):
        if value is None:
            return value
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            return domain(value)
