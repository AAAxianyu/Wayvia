"""Bounded HTTPS redundancy; identity and certificate failures never fall back.

Only one fresh, authenticated response matching the configured exit identity can
succeed. Attempts share the original deadline and never follow redirects or use
ambient proxy settings. Diagnostics contain codes/timings, never bodies or URLs.
"""

import contextlib
import ipaddress
import ssl
import subprocess
import time


def handshake_transport_closed(result):
    """Recognize curl/OpenSSL transport loss, not certificate or protocol failures.

    curl 35 also covers an EOF/reset before the target handshake completes. Keep
    unknown diagnostics terminal, and expose only a fixed code, never stderr.
    A retry still needs a new TLS-verified response with the expected identity.
    """
    diagnostic = getattr(result, "stderr", b"")
    if not isinstance(diagnostic, bytes):
        return False
    diagnostic = diagnostic[:8192].lower()
    if any(word in diagnostic for word in (b"certificate", b"x509", b"alert", b"verify")):
        return False
    return any(
        marker in diagnostic for marker in (b"ssl_error_syscall", b"unexpected eof while reading")
    )


def matches_body(body, expected, response_format):
    if response_format == "text":
        return body.strip() == expected
    if response_format != "cloudflare_trace":
        return False
    values = [line[3:] for line in body.splitlines() if line.startswith("ip=")]
    try:
        return len(values) == 1 and ipaddress.ip_address(values[0]) == ipaddress.ip_address(
            expected
        )
    except ValueError:
        return False


def observe_http(payload, *, connect=None):
    connect = connect or (lambda timeout: contextlib.nullcontext([]))
    urls = [payload["url"] if payload["kind"] == "trojan_http" else payload["target"]]
    urls.extend(payload.get("fallback_urls", ()))
    deadline = time.monotonic() + payload["timeout"]
    attempts = []
    error = "HTTP_DEADLINE"
    for index, url in enumerate(urls):
        started = time.monotonic()
        remaining = deadline - started
        if remaining <= 0:
            break
        # Reserve time for each configured alternative, within the same deadline.
        budget = remaining / (len(urls) - index)
        retry = False
        code = None
        status = None
        result = None
        transport_closed_after_response = False
        try:
            try:
                with connect(budget) as proxy:
                    result = subprocess.run(
                        [
                            "curl",
                            "--disable",
                            "--silent",
                            "--show-error",
                            "--fail",
                            "--noproxy",
                            "",
                            "--proxy",
                            "",
                            *proxy,
                            "--proto",
                            "=http,https",
                            "--max-filesize",
                            "16384",
                            "--max-time",
                            str(budget),
                            "--write-out",
                            "\n%{http_code}",
                            "--",
                            url,
                        ],
                        capture_output=True,
                        timeout=budget,
                        check=False,
                    )
            except ConnectionError:
                if result is None:
                    raise
                # Curl has completed. A relay close/reset during teardown must not
                # replace its result; TLS, status, identity and deadline checks below
                # still decide success. In particular, curl TLS failures stay fatal.
                transport_closed_after_response = True
            code = result.returncode
            body, status_text = result.stdout.decode("utf-8").rsplit("\n", 1)
            status = int(status_text)
            if code == 0 and status == 200:
                error = (
                    ""
                    if matches_body(
                        body, payload["expected"], payload.get("response_format", "text")
                    )
                    else "HTTP_IDENTITY_MISMATCH"
                )
            elif code == 35 and handshake_transport_closed(result):
                error, retry = "HTTP_TLS_TRANSPORT_FAILED", True
            elif code in {35, 51, 58, 59, 60, 64, 66, 77, 80, 82, 83, 90, 91}:
                error = "HTTP_TLS_FAILED"
            else:
                retry = code in {5, 6, 7, 18, 28, 52, 55, 56} or (
                    code == 22 and (status >= 500 or status == 429)
                )
                error = "HTTP_TIMEOUT" if code == 28 else "HTTP_REQUEST_FAILED"
        except ssl.SSLError:
            error = "HTTP_TLS_FAILED"
        except subprocess.TimeoutExpired:
            error, retry = "HTTP_TIMEOUT", True
        except (ConnectionError, TimeoutError):
            error, retry = "HTTP_TRANSPORT_FAILED", True
        except (OSError, ValueError):
            error = "HTTP_INVALID_RESPONSE"
        attempts.append(
            {
                "endpoint": index,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "curl_code": code,
                "http_status": status,
                "result": error or "healthy",
                **(
                    {"transport_closed_after_response": True}
                    if transport_closed_after_response
                    else {}
                ),
            }
        )
        if not error:
            if time.monotonic() >= deadline:
                error = "HTTP_DEADLINE"
                break
            return {"healthy": True, "check": "exit_identity", "attempts": attempts}
        if not retry:
            break
    return {"healthy": False, "check": "exit_identity", "error": error, "attempts": attempts}
