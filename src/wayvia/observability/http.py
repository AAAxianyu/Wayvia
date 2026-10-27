"""Bounded local emission. Failure never changes the subscription response."""

import json
import re
import socket
import time
import uuid

from wayvia.observability.models import Event, event


class Emitter:
    def __init__(self, path, host_id="ali", role="ingress"):
        self.path, self.host_id, self.role = str(path), host_id, role
        self.epoch = uuid.uuid4().hex
        self.dropped = 0

    def __call__(self, values):
        try:
            item = event(self.host_id, self.role, "api", self.epoch, "request", **values)
            if self.dropped and send(
                self.path,
                event(
                    self.host_id,
                    self.role,
                    "api",
                    self.epoch,
                    "gap",
                    code="queue_full",
                    lost_count=self.dropped,
                ),
            ):
                self.dropped = 0
            if not send(self.path, item):
                self.dropped += 1
        except Exception:
            self.dropped += 1


def send(path, item):
    try:
        payload = item.model_dump_json(exclude_none=True).encode()
        if len(payload) > 4096:
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.sendto(payload, str(path))
        return True
    except (OSError, ValueError):
        return False


def route_class(path):
    if re.fullmatch(r"/subscriptions/[^/]+/info", path):
        return "subscription_info"
    if re.fullmatch(r"/subscriptions/[^/]+/[^/]+", path):
        return "subscription_download"
    if path == "/healthz":
        return "health"
    if path.startswith(("/admin", "/api/")):
        return "management_denied"
    return "unknown_path"


def nginx_event(raw, host_id, role):
    # Syslog prefix is untrusted and discarded; only the closed JSON shape is used.
    start = raw.find(b'{"wayvia_log":1,')
    if start < 0 or len(raw) > 8192:
        raise ValueError("Invalid log envelope")
    data = json.loads(raw[start:])
    keys = {
        "wayvia_log",
        "request_id",
        "at",
        "ip",
        "method",
        "route",
        "format",
        "status",
        "bytes",
        "duration",
        "upstream",
        "rate",
        "concurrent",
    }
    if set(data) != keys:
        raise ValueError("Invalid log fields")

    def duration(value):
        try:
            return float(value) * 1000
        except (ValueError, TypeError):
            return None

    return Event(
        event_id=data["request_id"],
        host_id=host_id,
        role=role,
        source="nginx",
        source_epoch="nginx",
        observed_at=float(data["at"]),
        kind="request",
        request_id=data["request_id"],
        source_ip=data["ip"],
        method=data["method"],
        route=data["route"],
        format=data["format"],
        status=int(data["status"]),
        bytes_sent=int(data["bytes"]),
        duration_ms=duration(data["duration"]),
        upstream_ms=duration(data["upstream"]),
        limit_rate=data["rate"] or "NA",
        limit_concurrent=data["concurrent"] or "NA",
    )


def install_middleware(app, sink, trusted_proxy):
    @app.middleware("http")
    async def observe(request, call_next):
        started = time.monotonic()
        request.state.log_identity = {}
        ident = request.headers.get("x-wayvia-request-id", "")
        if not (
            trusted_proxy
            and request.client
            and request.client.host in {"127.0.0.1", "::1"}
            and re.fullmatch(r"[a-f0-9]{32}", ident)
        ):
            ident = uuid.uuid4().hex
        request.state.edge_request_id = ident
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            try:
                path = request.url.path
                if not path.startswith("/subscriptions/"):
                    raise ValueError("Not a subscription identity event")
                fmt = path.rsplit("/", 1)[-1]
                method = request.method
                sink(
                    {
                        "request_id": ident,
                        "route": route_class(path),
                        "format": fmt if fmt in {"mihomo", "sing-box"} else "unknown",
                        "method": method
                        if method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
                        else "OTHER",
                        "status": status,
                        "duration_ms": (time.monotonic() - started) * 1000,
                        **request.state.log_identity,
                    }
                )
            except Exception:
                pass  # Logging cannot turn a successful subscription into a failure.


def identify(request):
    def accept(device_id, subscription_id):
        request.state.log_identity = {
            "device_id": device_id,
            "subscription_id": subscription_id,
            "identity_source": "credential_only",
        }

    return accept
