"""The observer must redact credentials and preserve failed TLS/exit identity results."""

import importlib.util
import json
from pathlib import Path

import pytest


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parents[2] / "scripts" / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


observer = load("observe_stability")
summary = load("summarize_observation")


def test_monitor_redaction():
    private = "PRIVATE_CARRIER_TOKEN"
    clean = observer.sanitize_monitor(
        {
            "state": "ACTIVE",
            "updated_at": "2026-10-21T03:00:00+00:00",
            "plan": {"password": private},
            "evidence": [
                {
                    "layer": "access",
                    "status": "healthy",
                    "message": private,
                    "error": "bad " + private,
                    "attempts": [
                        {
                            "result": "healthy",
                            "body": private,
                            "transport_closed_after_response": True,
                        }
                    ],
                }
            ],
        }
    )
    assert private not in json.dumps(clean)
    assert clean["evidence"][0]["error"] == "REDACTED"
    assert clean["evidence"][0]["attempts"][0]["transport_closed_after_response"]
    assert observer.safe_code(private) == "REDACTED"
    assert observer.safe_code("HTTP_TIMEOUT") == "HTTP_TIMEOUT"


@pytest.mark.parametrize(
    "code,http,tls,body,expected,ok",
    [
        (0, 200, 0, "ip=203.0.113.1", "203.0.113.1", True),
        (28, 200, 0, "ip=203.0.113.1", "203.0.113.1", False),
        (0, 302, 0, "ip=203.0.113.1", "203.0.113.1", False),
        (60, 200, 20, "ip=203.0.113.1", "203.0.113.1", False),
        (0, 200, 0, "ip=203.0.113.2", "203.0.113.1", False),
        (0, 200, 0, "ip=203.0.113.1\nip=203.0.113.1", "203.0.113.1", False),
        (0, 200, 0, "ip=invalid", "203.0.113.1", False),
        (0, 200, 0, "empty", "203.0.113.1", False),
    ],
)
def test_controls_do_not_hide_failures(code, http, tls, body, expected, ok):
    raw = body + "\nWAYVIA_METRICS:" + json.dumps({"http_code": http, "ssl_verify_result": tls})
    parsed = observer.parse_http(code, raw, expected)
    assert summary.control_ok(parsed) is ok
    assert body not in json.dumps(parsed)


def test_observation_output_is_private_exclusive_and_bounded(tmp_path):
    path = tmp_path / "samples.jsonl"
    writer = observer.Writer(path, limit=100)
    writer.put({"type": "sample"})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        observer.Writer(path)
    with pytest.raises(observer.SizeLimitError):
        writer.put({"large": "x" * 200})
    writer.file.close()
    assert path.stat().st_size < 100


def test_dns_failure_not_counted_as_healthy():
    assert summary.control_ok({"exit": 0, "rcode": "NOERROR"})
    assert not summary.control_ok({"exit": 0, "rcode": "SERVFAIL"})
    assert not summary.control_ok({"exit": 9, "rcode": "NOERROR"})


def test_live_summary_accepts_only_final_partial_line(tmp_path):
    path = tmp_path / "samples.jsonl"
    path.write_text('{"type":"start","at":"now"}\n{"type":')
    result = summary.summarize(tmp_path)
    assert result["partial_lines_ignored"] == 1
    assert result["samples"] == 0
    path.write_text("not-json\n")
    with pytest.raises(json.JSONDecodeError):
        summary.summarize(tmp_path)
