"""Transient endpoint loss may fall back; identity and TLS failures must not."""

import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest
from test_topology import artifacts, probes

from wayvia.adapters.base import AdapterError
from wayvia.adapters.openvpn import check_safe_config
from wayvia.agent import http_probe
from wayvia.agent.models import Plan, Probe
from wayvia.agent.planner import build_plan
from wayvia.agent.validation import validate_probes


def payload():
    return dict(
        kind="http",
        target="https://1.1.1.1/cdn-cgi/trace",
        fallback_urls=["https://1.0.0.1/cdn-cgi/trace"],
        expected="203.0.113.8",
        response_format="cloudflare_trace",
        timeout=10,
    )


def result(code=0, body="ip=203.0.113.8\ncolo=NRT\n", status=200):
    return SimpleNamespace(returncode=code, stdout=f"{body}\n{status:03d}".encode())


def test_transient_timeout_uses_fresh_alternative_in_original_budget(monkeypatch):
    now = [100.0]
    calls = []
    monkeypatch.setattr(http_probe.time, "monotonic", lambda: now[0])

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if len(calls) == 1:
            now[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return result()

    monkeypatch.setattr(http_probe.subprocess, "run", run)
    observation = http_probe.observe_http(payload())
    assert observation["healthy"] is True
    assert [item[1]["timeout"] for item in calls] == [5, 5]
    assert calls[1][0][-1] == payload()["fallback_urls"][0]
    assert observation["attempts"][0]["result"] == "HTTP_TIMEOUT"
    assert observation["attempts"][1]["result"] == "healthy"
    assert all("--insecure" not in argv and "--location" not in argv for argv, _ in calls)
    assert "203.0.113.8" not in json.dumps(observation)


@pytest.mark.parametrize(
    "response,error",
    [
        (result(body="ip=203.0.113.9\n"), "HTTP_IDENTITY_MISMATCH"),
        (result(body="ip=203.0.113.8\nip=203.0.113.8\n"), "HTTP_IDENTITY_MISMATCH"),
        (result(body="no address"), "HTTP_IDENTITY_MISMATCH"),
        (result(60, "", 0), "HTTP_TLS_FAILED"),
        (result(35, "", 0), "HTTP_TLS_FAILED"),
        (result(0, "", 302), "HTTP_REQUEST_FAILED"),
        (result(22, "", 403), "HTTP_REQUEST_FAILED"),
        (result(63, "", 200), "HTTP_REQUEST_FAILED"),
    ],
)
def test_security_or_invalid_response_never_falls_back(monkeypatch, response, error):
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return response

    monkeypatch.setattr(http_probe.subprocess, "run", run)
    observation = http_probe.observe_http(payload())
    assert observation["healthy"] is False
    assert observation["error"] == error
    assert len(calls) == 1


def test_all_endpoints_unavailable_is_failed(monkeypatch):
    monkeypatch.setattr(http_probe.subprocess, "run", lambda *a, **k: result(28, "", 0))
    observation = http_probe.observe_http(payload())
    assert observation["healthy"] is False
    assert len(observation["attempts"]) == 2


def test_success_arriving_after_deadline_is_not_healthy(monkeypatch):
    now = [0]
    monkeypatch.setattr(http_probe.time, "monotonic", lambda: now[0])

    def run(*a, **k):
        now[0] = 11
        return result()

    monkeypatch.setattr(http_probe.subprocess, "run", run)
    assert http_probe.observe_http(payload())["error"] == "HTTP_DEADLINE"


@pytest.mark.parametrize(
    "changes",
    [
        {"fallback_urls": ["http://1.0.0.1/a"]},
        {"fallback_urls": ["https://user:pass@1.0.0.1/a"]},
        {"fallback_urls": ["https://1.0.0.1/a#fragment"]},
        {"fallback_urls": ["https://1.1.1.1/cdn-cgi/trace"]},
        {"expected": "not an IP"},
    ],
)
def test_structured_probe_inputs_are_restricted(changes):
    with pytest.raises(ValueError):
        Probe(layer="egress", **{**payload(), **changes})


def test_legacy_sealed_plan_digest_survives_optional_fields():
    plan = build_plan("ingress", "1", artifacts(), "baseline", probes=probes())
    legacy = plan.model_dump(mode="json", exclude={"digest"})
    for item in legacy["probes"]:
        item.pop("response_format")
        item.pop("fallback_urls")
    legacy["digest"] = hashlib.sha256(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    restored = Plan.model_validate(legacy)
    restored.check("baseline")
    changed = restored.model_copy(
        update={
            "probes": (
                restored.probes[0].model_copy(update={"fallback_urls": ("https://1.0.0.1/a",)}),
                *restored.probes[1:],
            )
        }
    )
    with pytest.raises(AdapterError, match="digest"):
        changed.check("baseline")


def test_access_and_egress_must_share_fallback_and_format():
    items = list(probes())
    items[0] = items[0].model_copy(update={"fallback_urls": ("https://1.0.0.1/a",)})
    with pytest.raises(AdapterError):
        validate_probes(build_plan("ingress", "1", artifacts(), "baseline", probes=items))


@pytest.mark.parametrize("flags", ["MARK 1", "TCP_NODELAY MARK 1", "", "TCP_NODELAY TCP_NODELAY"])
def test_openvpn_socket_flags_cannot_expand_privileges(flags):
    with pytest.raises(AdapterError):
        check_safe_config("socket-flags " + flags)
    check_safe_config("socket-flags TCP_NODELAY")
