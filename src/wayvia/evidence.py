"""Versioned, allowlisted evidence shared by probes and bounded observers.

These records describe observations, not health policy. Missing data stays null or
unknown. No URL, command output, plan, credential or response body belongs here.
"""

import hashlib
import math
import re
import time
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from wayvia import __version__

SCHEMA_VERSION = 1
LAYERS = {"access", "carrier", "vpn", "egress", "dns", "ipv6", "return"}
CODES = {
    "HTTP_DEADLINE",
    "HTTP_IDENTITY_MISMATCH",
    "HTTP_INVALID_RESPONSE",
    "HTTP_REQUEST_FAILED",
    "HTTP_TIMEOUT",
    "HTTP_TLS_FAILED",
    "HTTP_TLS_TRANSPORT_FAILED",
    "HTTP_TRANSPORT_FAILED",
    "PROTOCOL_PROBE_FAILED",
    "PROBE_DEADLINE",
    "PROBE_INVALID",
    "PROBE_UNSUPPORTED",
    "PROBE_UPGRADE_REQUIRED",
    "COMMAND_TIMEOUT",
    "COMMAND_FAILED",
    "COMPONENT_MISSING",
}
PHASES = {
    "unknown",
    "none",
    "connect",
    "tls",
    "first_byte",
    "response",
    "identity",
    "http_status",
    "observer_deadline",
    "relay_connect",
    "relay_tls",
    "relay_forward",
}
TERMINATIONS = {
    "unknown",
    "completed",
    "curl_timeout",
    "process_deadline",
    "helper_deadline",
    "verification_deadline",
    "transport_closed",
    "transport_timeout",
    "tls_failed",
    "invalid_response",
}
TIMINGS = ("connect_ms", "tls_ms", "first_byte_ms", "total_ms")
FAILURE_CLASSES = {
    "unknown",
    "none",
    "certificate",
    "tls",
    "identity",
    "timeout",
    "connection_closed",
    "request",
    "invalid_response",
}


def number(value):
    return (
        value
        if type(value) in {int, float} and 0 <= value < 10**20 and math.isfinite(value)
        else None
    )


def safe_time(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC).isoformat() if parsed.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def token(value, pattern):
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def choice(value, allowed, default="unknown"):
    return value if isinstance(value, str) and value in allowed else default


def safe_code(value):
    return choice(value, CODES, "REDACTED")


def boot_id():
    try:
        return token(Path("/proc/sys/kernel/random/boot_id").read_text().strip(), r"[a-f0-9-]{36}")
    except OSError:
        return None


@lru_cache(maxsize=1)
def producer_digest():
    root = Path(__file__).parent
    names = [
        "evidence.py",
        "observation_run.py",
        "agent/http_probe.py",
        "agent/http_diagnostics.py",
        "agent/probes.py",
        "agent/probe_runtime.py",
        "agent/transactions.py",
    ]
    digest = hashlib.sha256()
    try:
        for name in names:
            digest.update(name.encode())
            digest.update((root / name).read_bytes())
    except OSError:
        return None
    return digest.hexdigest()


def context(
    *, role=None, revision=None, generation=None, run_id=None, observed_at=None, monotonic_ms=None
):
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": uuid.uuid4().hex,
        "observed_at": safe_time(observed_at) or datetime.now(UTC).isoformat(),
        "monotonic_ms": round(time.monotonic() * 1000, 3)
        if monotonic_ms is None
        else number(monotonic_ms),
        "role": choice(role, {"ingress", "exit"}),
        "revision": token(revision, r"[a-f0-9]{64}"),
        "generation": token(generation, r"[a-f0-9]{32}"),
        "run_id": token(run_id, r"[a-zA-Z0-9-]{1,64}"),
        "boot_id": boot_id(),
        "software_version": __version__,
        "producer_digest": producer_digest(),
        "clock_sync": "unknown",
        "clock_offset_ms": None,
    }


def sanitize_context(row):
    """Project existing evidence without minting new timestamps or freshness."""
    return {
        "schema_version": SCHEMA_VERSION if row.get("schema_version") == SCHEMA_VERSION else None,
        "event_id": token(row.get("event_id"), r"[a-f0-9]{32}"),
        "observed_at": safe_time(row.get("observed_at")),
        "monotonic_ms": number(row.get("monotonic_ms")),
        "role": choice(row.get("role"), {"ingress", "exit"}),
        "revision": token(row.get("revision"), r"[a-f0-9]{64}"),
        "generation": token(row.get("generation"), r"[a-f0-9]{32}"),
        "run_id": token(row.get("run_id"), r"[a-zA-Z0-9-]{1,64}"),
        "boot_id": token(row.get("boot_id"), r"[a-f0-9-]{36}"),
        "software_version": token(row.get("software_version"), r"[0-9]+\.[0-9]+\.[0-9]+"),
        "producer_digest": token(row.get("producer_digest"), r"[a-f0-9]{64}"),
        "clock_sync": choice(row.get("clock_sync"), {"synchronized", "unsynchronized"}),
        "clock_offset_ms": None,  # No offset measurement is implemented yet.
    }


def sanitize_attempt(row):
    relay = row.get("relay")
    relay = relay if isinstance(relay, dict) else {}
    return {
        "endpoint": number(row.get("endpoint")),
        "duration_ms": number(row.get("duration_ms")),
        "curl_code": number(row.get("curl_code")),
        "http_status": number(row.get("http_status")),
        "result": "healthy" if row.get("result") == "healthy" else safe_code(row.get("result")),
        "transport_closed_after_response": row.get("transport_closed_after_response") is True,
        "termination": choice(row.get("termination"), TERMINATIONS),
        "failure_stage": choice(row.get("failure_stage"), PHASES),
        "failure_class": choice(row.get("failure_class"), FAILURE_CLASSES),
        "connect_scope": choice(row.get("connect_scope"), {"target", "local_relay"}),
        "metrics_source": choice(
            row.get("metrics_source"), {"curl_write_out", "partial_write_out"}
        ),
        "response_complete": row.get("response_complete")
        if type(row.get("response_complete")) is bool
        else None,
        **{key: number(row.get(key)) for key in TIMINGS},
        "relay": {
            **{key: number(relay.get(key)) for key in ("connect_ms", "tls_ms", "tunnel_ready_ms")},
            "stage": choice(relay.get("stage"), PHASES),
            "cleanup": choice(
                relay.get("cleanup"), {"ok", "connection_closed", "tls_error", "error"}
            ),
        },
    }
