"""Offline command doubles exercise timing accuracy, budgets and redaction."""

import contextlib
import json
import runpy
import ssl
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_http_probe_resilience import payload

from wayvia.adapters.base import AdapterError, CommandResult
from wayvia.agent import http_probe
from wayvia.agent.http_diagnostics import MARKER
from wayvia.agent.models import Probe
from wayvia.agent.probe_runtime import RelayOptions
from wayvia.agent.probes import verify

observer = SimpleNamespace(
    **runpy.run_path(str(Path(__file__).parents[2] / "scripts/observe_stability.py"))
)


def test_relay_timeout_is_not_reported_as_connection_close():
    @contextlib.contextmanager
    def timeout(budget):
        raise TimeoutError("private relay address")
        yield []

    attempt = http_probe.observe_http(single_payload(), connect=timeout)["attempts"][0]
    assert attempt["termination"] == "transport_timeout"
    assert attempt["failure_class"] == "timeout"
    assert "private" not in json.dumps(attempt)


@pytest.mark.parametrize(
    "code,body,error_class",
    [
        (60, b"", "certificate"),
        (35, b"", "tls"),
        (0, b"ip=203.0.113.9\n", "identity"),
    ],
)
def test_certificate_tls_and_identity_failures_remain_distinct(
    monkeypatch, code, body, error_class
):
    monkeypatch.setattr(
        http_probe.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=code, stdout=output(status=200 if code == 0 else 0, body=body)
        ),
    )
    result = http_probe.observe_http(single_payload())
    assert not result["healthy"]
    assert result["attempts"][0]["failure_class"] == error_class


def output(*, connect=0, tls=0, first=0, total=1, status=0, body=b"PRIVATE_BODY"):
    return (
        body
        + MARKER
        + json.dumps(
            {
                "http_code": status,
                "time_connect": connect,
                "time_appconnect": tls,
                "time_starttransfer": first,
                "time_total": total,
            }
        ).encode()
    )


def single_payload():
    return {**payload(), "fallback_urls": []}


@pytest.mark.parametrize(
    "timings,stage",
    [
        ({}, "connect"),
        ({"connect": 0.1}, "tls"),
        ({"connect": 0.1, "tls": 0.2}, "first_byte"),
        ({"connect": 0.1, "tls": 0.2, "first": 0.3}, "response"),
    ],
)
def test_curl_timeout_preserves_milestones_and_failure_phase(monkeypatch, timings, stage):
    monkeypatch.setattr(
        http_probe.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=28, stdout=output(**timings), stderr=b"https://secret.example/password"
        ),
    )
    result = http_probe.observe_http(single_payload())
    attempt = result["attempts"][0]
    assert result["healthy"] is False and result["error"] == "HTTP_TIMEOUT"
    assert attempt["failure_stage"] == stage and attempt["termination"] == "curl_timeout"
    assert attempt["response_complete"] is False and attempt["total_ms"] == 1000
    assert attempt["tls_ms"] == (200 if "tls" in timings else None)
    assert "PRIVATE" not in json.dumps(result) and "secret.example" not in json.dumps(result)


def test_missing_milestones_remain_unknown(monkeypatch):
    monkeypatch.setattr(
        http_probe.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=28, stdout=MARKER + b'{"http_code":0}'),
    )
    attempt = http_probe.observe_http(single_payload())["attempts"][0]
    assert attempt["failure_stage"] == "unknown"
    assert all(attempt[k] is None for k in ("connect_ms", "tls_ms", "first_byte_ms", "total_ms"))


def test_process_timeout_retains_output_but_never_accepts_identity(monkeypatch):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(
            argv,
            kwargs["timeout"],
            output=output(connect=0.1, tls=0.2, first=0.3, status=200, body=b"ip=203.0.113.8\n"),
        )

    monkeypatch.setattr(http_probe.subprocess, "run", timeout)
    result = http_probe.observe_http(single_payload())
    attempt = result["attempts"][0]
    assert result["healthy"] is False and attempt["response_complete"] is False
    assert attempt["termination"] == "process_deadline"
    assert attempt["failure_stage"] == "observer_deadline"
    assert attempt["first_byte_ms"] == 300 and attempt["metrics_source"] == "partial_write_out"


def test_unknown_process_timeout_never_invents_stages(monkeypatch):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"secret response fragment")

    monkeypatch.setattr(http_probe.subprocess, "run", timeout)
    attempt = http_probe.observe_http(single_payload())["attempts"][0]
    assert attempt["connect_ms"] is None and attempt["metrics_source"] == "unknown"
    assert "secret" not in json.dumps(attempt)


def test_relay_and_target_stages_are_separate(monkeypatch):
    @contextlib.contextmanager
    def connect(timeout):
        yield RelayOptions(12345, {"connect_ms": 20, "stage": "relay_tls"})

    monkeypatch.setattr(
        http_probe.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=28, stdout=output(connect=0.001)),
    )
    data = {**single_payload(), "kind": "trojan_http", "url": payload()["target"]}
    attempt = http_probe.observe_http(data, connect=connect)["attempts"][0]
    assert attempt["connect_scope"] == "local_relay" and attempt["connect_ms"] == 1
    assert attempt["relay"]["connect_ms"] == 20 and attempt["relay"]["tls_ms"] is None
    assert attempt["failure_stage"] == "relay_tls"


def test_relay_cleanup_tls_error_keeps_completed_request_evidence(monkeypatch):
    @contextlib.contextmanager
    def connect(timeout):
        yield []
        raise ssl.SSLError("private cleanup detail")

    monkeypatch.setattr(
        http_probe.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout=output(connect=0.1, tls=0.2, first=0.3, status=200, body=b"ip=203.0.113.8\n"),
        ),
    )
    result = http_probe.observe_http(single_payload(), connect=connect)
    attempt = result["attempts"][0]
    assert not result["healthy"] and result["error"] == "HTTP_TLS_FAILED"
    assert attempt["response_complete"] is True and attempt["first_byte_ms"] == 300
    assert attempt["relay"]["cleanup"] == "tls_error"


def test_connect_setup_and_curl_share_the_same_budget(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(http_probe.time, "monotonic", lambda: now[0])

    @contextlib.contextmanager
    def connect(timeout):
        now[0] += 3
        yield []

    def command(argv, **kwargs):
        assert kwargs["timeout"] == 7
        assert float(argv[argv.index("--max-time") + 1]) == 7
        now[0] += 7
        return SimpleNamespace(
            returncode=0,
            stdout=output(connect=0.1, tls=0.2, first=0.3, status=200, body=b"ip=203.0.113.8\n"),
        )

    monkeypatch.setattr(http_probe.subprocess, "run", command)
    result = http_probe.observe_http(single_payload(), connect=connect)
    assert not result["healthy"] and result["error"] == "HTTP_DEADLINE"
    assert result["attempts"][0]["result"] == "HTTP_DEADLINE"


def test_outer_helper_timeout_has_its_own_cause(tmp_path):
    def command(*a, **k):
        raise AdapterError("COMMAND_TIMEOUT", "private command output")

    executor = SimpleNamespace(config_dir=tmp_path, _cmd=command)
    selected = Probe(layer="egress", **single_payload())
    result = verify(executor, [selected])[0]
    assert result["termination"] == "helper_deadline"
    assert result["failure_stage"] == "observer_deadline" and result["status"] == "failed"
    assert "private" not in json.dumps(result)


def test_invalid_helper_fields_cannot_leak_through_error_path(tmp_path):
    executor = SimpleNamespace(
        config_dir=tmp_path,
        _cmd=lambda *a, **k: CommandResult(
            0,
            json.dumps(
                {
                    "healthy": True,
                    "attempts": "PRIVATE",
                    "error": "PRIVATE",
                    "check": "PRIVATE",
                    "peer_count": "PRIVATE",
                }
            ),
        ),
    )
    result = verify(executor, [Probe(layer="egress", **single_payload())])[0]
    assert result["status"] == "failed" and result["error"] == "PROBE_INVALID"
    assert "PRIVATE" not in json.dumps(result)


def test_phase_evidence_survives_verify_and_observer_without_private_fields(tmp_path):
    attempt = {
        "endpoint": 0,
        "result": "HTTP_TIMEOUT",
        "connect_ms": 20,
        "tls_ms": 30,
        "first_byte_ms": None,
        "failure_stage": "first_byte",
        "termination": "curl_timeout",
        "response_complete": False,
        "stderr": "SECRET",
        "url": "SECRET",
        "body": "SECRET",
        "relay": {"connect_ms": 2, "password": "SECRET"},
    }
    executor = SimpleNamespace(
        config_dir=tmp_path,
        _cmd=lambda *a, **k: CommandResult(
            0, json.dumps({"healthy": False, "attempts": [attempt]})
        ),
    )
    evidence = verify(executor, [Probe(layer="egress", **single_payload())])
    row = evidence[0]
    row.update(role="ingress", generation="a" * 32, revision="b" * 64)
    sanitized = observer.sanitize_monitor({"state": "DEGRADED", "evidence": evidence})["evidence"][
        0
    ]
    assert sanitized["event_id"] == row["event_id"]
    assert sanitized["observed_at"] == row["observed_at"]
    assert sanitized["generation"] == "a" * 32
    assert sanitized["attempts"][0]["tls_ms"] == 30
    assert sanitized["attempts"][0]["failure_stage"] == "first_byte"
    assert "SECRET" not in json.dumps(sanitized)


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b'{"http_code":200,"time_total":NaN}',
        b'{"http_code":200,"time_connect":2,"time_total":1}',
    ],
)
def test_malformed_telemetry_cannot_fabricate_success(monkeypatch, raw):
    monkeypatch.setattr(
        http_probe.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"ip=203.0.113.8\n" + MARKER + raw),
    )
    # Nonfinite timings are projected as unknown; they do not prove a milestone.
    result = http_probe.observe_http(single_payload())
    if result["healthy"]:
        assert result["attempts"][0]["total_ms"] is None
    else:
        assert result["error"] == "HTTP_INVALID_RESPONSE"
