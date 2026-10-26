"""Explicit, bounded measurements through a local HTTP proxy. No route/core changes.

The local client's rules determine the route; measurements do not certify egress
or the server's VPN-over-proxy layers. Software tests inject MockTransport only.
"""

import json
import subprocess
import sys
import time

import httpx

from wayvia.client import TEST_URL, https_url
from wayvia.errors import WayviaError

BANDWIDTH_URL = "https://speed.cloudflare.com/__down?bytes=8388608"


def measure(
    *,
    kind="latency",
    proxy_port=7890,
    authorized=False,
    transport=None,
    clock=time.monotonic,
    max_bytes=8 * 1024 * 1024,
    seconds=10,
):
    if not authorized:
        raise WayviaError(
            "CLIENT_NETWORK_NOT_AUTHORIZED",
            "Explicit network measurement authorization is required",
        )
    if (
        kind not in {"latency", "throughput"}
        or type(proxy_port) is not int
        or not 1 <= proxy_port <= 65535
        or type(max_bytes) is not int
        or not 1024 <= max_bytes <= 8 * 1024 * 1024
        or not 1 <= seconds <= 10
    ):
        raise WayviaError("CLIENT_MEASUREMENT_INVALID", "Invalid bounded measurement options")
    if transport is None:
        payload = {
            "kind": kind,
            "proxy_port": proxy_port,
            "max_bytes": max_bytes,
            "seconds": seconds,
        }
        try:
            result = subprocess.run(
                [sys.executable, "-m", "wayvia.client.measure"],
                input=json.dumps(payload).encode(),
                capture_output=True,
                timeout=seconds,
            )
            if result.returncode or len(result.stdout) > 8192:
                raise ValueError()
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            return {
                "schema_version": 1,
                "status": "unavailable",
                "kind": kind,
                "error": "CLIENT_MEASUREMENT_DEADLINE",
                "https_available": None,
                "latency_ms": None,
                "download_mbps": None,
                "response_complete": False,
                "limits": {
                    "seconds": seconds,
                    "download_bytes": max_bytes,
                    "route": "local_proxy_policy",
                    "full_server_path": "not_verified",
                },
            }
        except (OSError, ValueError):
            raise WayviaError(
                "CLIENT_MEASUREMENT_HELPER_FAILED", "Measurement helper failed"
            ) from None
    return _measure(
        kind=kind,
        proxy_port=proxy_port,
        transport=transport,
        clock=clock,
        max_bytes=max_bytes,
        seconds=seconds,
    )


def _measure(
    *, kind, proxy_port, transport=None, clock=time.monotonic, max_bytes=8 * 1024 * 1024, seconds=10
):
    url = TEST_URL if kind == "latency" else BANDWIDTH_URL
    https_url(url)
    start, size = clock(), 0
    first_byte = None
    outcome, error, complete = "unavailable", None, False
    try:
        # A supplied transport is a software-test seam. Runtime always uses this
        # explicit loopback proxy with certificate verification and no direct fallback.
        options = (
            {"transport": transport}
            if transport is not None
            else {"proxy": f"http://127.0.0.1:{proxy_port}"}
        )
        with httpx.Client(
            **options,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(min(3, seconds)),
            verify=True,
        ) as client:
            with client.stream(
                "GET",
                url,
                headers={"Accept-Encoding": "identity", "User-Agent": "Wayvia-client-check/1"},
            ) as response:
                wanted = 204 if kind == "latency" else 200
                if response.status_code != wanted:
                    error = "CLIENT_TARGET_STATUS"
                elif response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    error = "CLIENT_UNEXPECTED_ENCODING"
                elif clock() - start > seconds:
                    error = "CLIENT_MEASUREMENT_DEADLINE"
                else:
                    for block in response.iter_raw():
                        now = clock()
                        if first_byte is None:
                            first_byte = now
                        if now - start > seconds:
                            error = "CLIENT_MEASUREMENT_DEADLINE"
                            break
                        take = min(len(block), max_bytes - size)
                        size += take
                        if size >= max_bytes:
                            error = "CLIENT_MEASUREMENT_BYTE_LIMIT"
                            break
                    else:
                        complete = True
                    if error is None or error == "CLIENT_MEASUREMENT_BYTE_LIMIT":
                        if kind == "throughput" and size == 0:
                            error = "CLIENT_MEASUREMENT_EMPTY_PAYLOAD"
                        else:
                            outcome = "measured"
    except httpx.TimeoutException:
        error = "CLIENT_PROXY_OR_TARGET_TIMEOUT"
    except (httpx.HTTPError, OSError):
        error = "CLIENT_PROXY_OR_TARGET_FAILED"
    elapsed = max(0, clock() - start)
    return {
        "schema_version": 1,
        "kind": kind,
        "status": outcome,
        "error": error,
        "duration_ms": round(elapsed * 1000, 3),
        "download_bytes": size,
        "https_available": True if outcome == "measured" else None,
        "first_payload_ms": round((first_byte - start) * 1000, 3)
        if first_byte is not None
        else None,
        "response_complete": complete,
        "latency_ms": round(elapsed * 1000, 3)
        if kind == "latency" and outcome == "measured"
        else None,
        "download_mbps": round(size * 8 / elapsed / 1000000, 3)
        if kind == "throughput" and elapsed > 0 and outcome == "measured"
        else None,
        "limits": {
            "seconds": seconds,
            "download_bytes": max_bytes,
            "cadence": "one_shot_no_background_task",
            "route": "local_proxy_policy",
            "egress_identity": "unknown",
            "full_server_path": "not_verified",
            "upload_speed": "not_measured",
            "rate_scope": "payload_over_total_request_time",
            "latency_scope": "total_request_time_distinct_from_mihomo_unified_RTT",
        },
    }


if __name__ == "__main__":
    try:
        request = json.loads(sys.stdin.buffer.read(1024))
        print(json.dumps(_measure(**request)))
    except Exception:
        print(json.dumps({"status": "unavailable", "error": "CLIENT_MEASUREMENT_HELPER_FAILED"}))
