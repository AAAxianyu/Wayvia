"""Curl write-out timings are cumulative from curl start, not phase durations."""

import json

from wayvia.evidence import number

MARKER = b"\nWAYVIA_METRICS:"
WRITE_OUT = (
    '\nWAYVIA_METRICS:{"http_code":%{http_code},"time_connect":%{time_connect},'
    '"time_appconnect":%{time_appconnect},"time_starttransfer":%{time_starttransfer},'
    '"time_total":%{time_total}}'
)
FIELDS = {
    "connect_ms": "time_connect",
    "tls_ms": "time_appconnect",
    "first_byte_ms": "time_starttransfer",
    "total_ms": "time_total",
}


def parse_output(output):
    """Never return raw diagnostics. A missing/zero milestone is not completed."""
    if not isinstance(output, bytes) or len(output) > 20000:
        raise ValueError("invalid probe output")
    body, raw = output.rsplit(MARKER, 1)
    # Older curl formats HTTP 000 as three zeroes, which JSON does not accept.
    raw = raw.replace(b'"http_code":000', b'"http_code":0', 1)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("invalid timings")
    status = data["http_code"]
    if type(status) is not int or not 0 <= status <= 599:
        raise ValueError("invalid HTTP status")
    metrics = {}
    for key, source in FIELDS.items():
        value = number(data.get(source))
        metrics[key] = round(value * 1000, 3) if value is not None and value > 0 else None
    # Malformed or inconsistent telemetry cannot create completed stages.
    milestones = [metrics[k] for k in FIELDS if metrics[k] is not None]
    if milestones != sorted(milestones):
        raise ValueError("nonmonotonic probe timings")
    metrics["connect_reported_incomplete"] = number(data.get("time_connect")) == 0
    return body.decode("utf-8"), status, metrics


def failure_stage(metrics, *, tls, proxy, code, status, error, relay):
    if not error:
        return "none"
    if error == "HTTP_IDENTITY_MISMATCH":
        return "identity"
    if code == 22:
        return "http_status"
    if proxy and relay.get("stage") in {"relay_connect", "relay_tls"}:
        return relay["stage"]
    if error == "HTTP_TLS_FAILED":
        return "tls"
    if status and status != 200:
        return "http_status"
    if metrics.get("first_byte_ms") is not None:
        return "response"
    if metrics.get("tls_ms") is not None:
        return "first_byte"
    if code in {35, 51, 58, 59, 60, 64, 66, 77, 80, 82, 83, 90, 91}:
        return "tls"
    if metrics.get("connect_ms") is not None:
        # Connecting to the local SOCKS socket proves nothing about the target.
        return "unknown" if proxy else ("tls" if tls else "first_byte")
    if code == 7 or (metrics.get("connect_reported_incomplete") and code in {5, 6, 28}):
        return "connect"
    return "unknown"
