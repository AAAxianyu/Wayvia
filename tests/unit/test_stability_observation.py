"""The observer must redact credentials and preserve failed TLS/exit identity results."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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


def test_summary_reads_resumed_segments_and_marks_observer_deadline_unknown(tmp_path):
    (tmp_path / "samples.jsonl").write_text('{"type":"start","at":"old"}\n{"partial":')
    second = [
        {"type": "resume", "at": "later", "gap_after": "old"},
        {
            "type": "control",
            "at": "later",
            "label": "test",
            "observation_ended": True,
            "result": {"exit": 124},
        },
    ]
    (tmp_path / "samples-000002.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in second)
    )
    result = summary.summarize(tmp_path)
    assert len(result["segments"]) == 2 and len(result["resumptions"]) == 1
    assert result["partial_lines_ignored"] == 1 and result["host_global_tcp_delta"] is None
    assert result["controls"]["test"]["unknown_observation_ended"] == 1
    assert result["controls"]["test"]["failed"] == 0


def test_expired_or_stopped_observer_starts_no_command(monkeypatch):
    monkeypatch.setattr(
        observer.subprocess, "run", lambda *a, **k: pytest.fail("command after end")
    )
    budget = SimpleNamespace(remaining=lambda: 0)
    row = observer.control_job("test", "http", "192.0.2.1", None, None, budget=budget)
    assert row["observation_ended"] is True
    assert row["result"]["valid_https_response"] is False


def test_observer_commands_shrink_to_remaining_budget(monkeypatch):
    calls = []

    def command(argv, **kwargs):
        calls.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(observer.subprocess, "run", command)
    observer.command(["unused"], timeout=5, budget=SimpleNamespace(remaining=lambda: 0.125))
    assert calls == [0.125]


def test_collector_signal_path_preserves_interruption_and_explicit_stop(tmp_path, monkeypatch):
    from wayvia.observation_run import ObservationRun, request_stop

    handlers = {}

    def install(sig, handler):
        previous = handlers.get(sig)
        handlers[sig] = handler
        return previous

    monkeypatch.setattr(observer.signal, "signal", install)
    monkeypatch.setattr(
        observer.subprocess, "run", lambda *a, **k: pytest.fail("command after signal")
    )

    def host_metrics():
        # Invoke the registered handler as a software event, without sending an OS
        # signal or stopping any real service/process.
        handlers[observer.signal.SIGTERM](observer.signal.SIGTERM, None)
        return {}

    monkeypatch.setattr(observer, "host_metrics", host_metrics)
    contract = {
        "role": "ingress",
        "revision": "b" * 64,
        "generation": "c" * 32,
        "until": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        "max_bytes": 16384,
        "sample_interval_seconds": 5,
    }
    runtime, journal = tmp_path / "runtime.json", tmp_path / "journal.json"
    runtime.write_text(json.dumps({"transaction_id": contract["generation"]}))
    journal.write_text(json.dumps({"plan": {"revision": contract["revision"]}}))
    args = SimpleNamespace(
        generation=contract["generation"], role="ingress", peer_ip="192.0.2.1", interface="test"
    )
    root = tmp_path / "observations"
    with ObservationRun(root, "signal", contract) as run:
        assert observer.collect(args, run, journal, runtime, "192.0.2.2") == 2
    assert json.loads((root / "signal" / "result.json").read_text())["reason"] == "interrupted"
    assert not (root / "signal" / "stop.json").exists()
    with ObservationRun(root, "signal", contract, resume=True) as run:
        request_stop(root, "signal")
        assert observer.collect(args, run, journal, runtime, "192.0.2.2") == 0
    assert json.loads((root / "signal" / "result.json").read_text())["reason"] == "user_stop"
