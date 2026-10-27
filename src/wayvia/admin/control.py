"""Fixed local control-plane gateway; never accepts URLs, headers or commands from UI."""

import re

import httpx

from wayvia.observability.models import LogError

ID = re.compile(r"^[a-f0-9]{32}$")


class ControlGateway:
    def __init__(self, token, *, client=None):
        if len(token) < 32:
            raise LogError("ADMIN_CONTROL_SECRET")
        self.token = token
        self.client = client or httpx.Client(
            base_url="http://127.0.0.1:8787",
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(5, connect=1),
            limits=httpx.Limits(max_connections=2),
        )

    def request(self, method, path, *, body=None, key=None, params=None):
        headers = {"Authorization": "Bearer " + self.token}
        if key:
            headers["Idempotency-Key"] = key
        try:
            # TestClient and httpx both support this bounded streaming API.
            with self.client.stream(
                method, path, headers=headers, json=body, params=params
            ) as response:
                raw = bytearray()
                for block in response.iter_bytes():
                    raw.extend(block)
                    if len(raw) > 2097152:
                        raise LogError("ADMIN_CONTROL_LIMIT", status=503)
                if (
                    response.status_code >= 400
                    or response.status_code < 200
                    or response.status_code >= 300
                ):
                    raise LogError(
                        "ADMIN_CONTROL_REJECTED", status=409 if response.status_code < 500 else 503
                    )
                import json

                return json.loads(raw)
        except (httpx.HTTPError, ValueError):
            raise LogError("ADMIN_CONTROL_UNAVAILABLE", status=503) from None

    def read(self, kind, *, device=None, since=None, until=None, grain="minute"):
        if kind in {"status", "devices", "subscriptions", "config/publication"}:
            return self.request("GET", "/api/v1/" + kind)
        if kind == "history" and ID.fullmatch(device or "") and grain in {"minute", "hour"}:
            if (
                not isinstance(since, (int, float))
                or not isinstance(until, (int, float))
                or until - since > 86400 * (30 if grain == "minute" else 365)
            ):
                raise LogError("LOG_QUERY_WINDOW")
            return self.request(
                "GET",
                f"/api/v1/devices/{device}/history",
                params={"since": since, "until": until, "grain": grain},
            )
        raise LogError("ADMIN_OPERATION_INVALID")

    def action(self, action, obj, payload, key):
        mapping = {
            "device.add": ("POST", "/api/v1/devices"),
            "subscription.issue": ("POST", "/api/v1/subscriptions"),
        }
        if action in mapping:
            method, path = mapping[action]
        elif ID.fullmatch(obj or "") and action in {
            "device.rotate",
            "device.revoke",
            "subscription.revoke",
        }:
            method = "DELETE" if action == "subscription.revoke" else "POST"
            path = (
                f"/api/v1/subscriptions/{obj}"
                if method == "DELETE"
                else f"/api/v1/devices/{obj}/{action.split('.')[1]}"
            )
        else:
            raise LogError("ADMIN_OPERATION_INVALID")
        return self.request(method, path, body=payload or None, key=key)
