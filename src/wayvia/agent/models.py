"""The local privileged protocol is a closed set of frozen structured actions."""

import hashlib
import ipaddress
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FILES = {
    "ingress.json",
    "bridge.json",
    "carrier.json",
    "exit.json",
    "vpn-client.conf",
    "vpn-server.conf",
    "manifest.json",
}
UNITS = {
    "wayvia-ingress.service",
    "wayvia-bridge.service",
    "wayvia-carrier.service",
    "wayvia-exit.service",
    "wayvia-vpn-client.service",
    "wayvia-vpn-server.service",
}
LAYERS = {"access", "carrier", "vpn", "egress", "dns", "ipv6", "return"}


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Action(Frozen):
    kind: Literal["install_config", "namespace", "veth", "guard", "service", "exit_nat"]
    resource: str
    content: str | None = None
    operation: Literal["ensure", "restart", "stop"] = "ensure"
    namespace: Literal["wayvia-egress"] | None = None
    interface: str | None = None
    subnet: str | None = None

    @model_validator(mode="after")
    def restrict(self):
        allowed = {
            "install_config": FILES,
            "namespace": {"wayvia-egress"},
            "veth": {"wayvia-host"},
            "guard": {"wayvia_guard"},
            "service": UNITS,
            "exit_nat": {"wayvia_exit"},
        }
        if self.resource not in allowed[self.kind]:
            raise ValueError("resource is outside Wayvia ownership")
        if self.kind == "install_config" and (
            self.content is None or len(self.content) > 1024 * 1024
        ):
            raise ValueError("bounded config content is required")
        if self.kind != "install_config" and self.content is not None:
            raise ValueError("raw content is not permitted for network or service actions")
        if self.interface and not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,14}", self.interface):
            raise ValueError("invalid network interface")
        if self.kind == "exit_nat" and not self.interface:
            raise ValueError("exit NAT needs the explicit physical egress interface")
        if self.kind == "exit_nat":
            network = ipaddress.ip_network(self.subnet or "", strict=True)
            if (
                network.version != 4
                or not network.is_private
                or network.prefixlen < 16
                or network.overlaps(ipaddress.ip_network("10.89.0.0/30"))
            ):
                raise ValueError("invalid or conflicting private VPN subnet")
        return self


class Probe(Frozen):
    layer: Literal["access", "carrier", "vpn", "egress", "dns", "ipv6", "return"]
    kind: Literal["service", "tcp", "route", "http", "dns", "ipv6_guard", "unsupported"]
    target: str = ""
    port: int = Field(default=0, ge=0, le=65535)
    expected: str = ""
    namespace: Literal["wayvia-egress"] | None = None
    timeout: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def restrict(self):
        if self.kind == "service" and self.target not in UNITS:
            raise ValueError("probe unit is not owned")
        if self.kind == "http" and (
            not self.target.startswith(("https://", "http://")) or not self.expected
        ):
            raise ValueError("HTTP probe requires a URL and expected exit address/body")
        if self.target.startswith("-") or any(c in self.target for c in "\n\r\x00"):
            raise ValueError("invalid probe target")
        if self.kind in {"tcp", "dns"} and (not self.target or not self.port):
            raise ValueError("probe requires target and port")
        if self.kind in {"tcp", "dns", "route"}:
            ipaddress.ip_address(self.target)
        if self.kind == "dns" and not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", self.expected
        ):
            raise ValueError("DNS probe needs a domain name")
        return self


class Plan(Frozen):
    schema_version: Literal[1] = 1
    role: Literal["ingress", "exit"]
    revision: str
    baseline: str
    created_at: datetime
    expires_at: datetime
    confirm_seconds: int = Field(default=120, ge=10, le=600)
    actions: tuple[Action, ...]
    probes: tuple[Probe, ...]
    digest: str = ""

    @model_validator(mode="after")
    def dates(self):
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("plan timestamps must have a timezone")
        if self.expires_at <= self.created_at:
            raise ValueError("invalid validity window")
        if (self.expires_at - self.created_at).total_seconds() > 900:
            raise ValueError("plan validity must not exceed 15 minutes")
        if len({(a.kind, a.resource, a.namespace) for a in self.actions}) != len(self.actions):
            raise ValueError("duplicate action resource")
        if len({p.layer for p in self.probes}) != len(self.probes):
            raise ValueError("duplicate probe layer")
        return self

    def computed_digest(self):
        value = self.model_dump(mode="json", exclude={"digest"})
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def seal(self):
        return self.model_copy(update={"digest": self.computed_digest()})

    def check(self, baseline, now=None):
        from wayvia.adapters.base import AdapterError

        if self.digest != self.computed_digest():
            raise AdapterError("PLAN_TAMPERED", "Plan digest does not match immutable contents")
        if (now or datetime.now(UTC)) >= self.expires_at:
            raise AdapterError("PLAN_EXPIRED", "Plan has expired; inspect and plan again")
        if self.created_at > (now or datetime.now(UTC)):
            raise AdapterError("PLAN_FUTURE", "Plan creation time is in the future")
        if baseline != self.baseline:
            raise AdapterError("BASELINE_CHANGED", "Host resources changed after planning")


class State(StrEnum):
    APPLYING = "APPLYING"
    VERIFYING = "VERIFYING"
    AWAITING_CONFIRM = "AWAITING_CONFIRM"
    ACTIVE = "ACTIVE"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    DEGRADED = "DEGRADED"
    SUPERSEDED = "SUPERSEDED"
