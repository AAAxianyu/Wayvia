import copy
import json
import subprocess

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from wayvia.cli import app
from wayvia.client import NODE
from wayvia.client.measure import measure
from wayvia.client.profiles import inspect, read_profile, stage
from wayvia.config import WayviaConfig, example_config
from wayvia.errors import WayviaError
from wayvia.render import ProfileRenderer


@pytest.fixture(scope="module")
def profile():
    data = example_config()
    data["policy"]["domestic_domain_rules_ref"] = None
    data["rulesets"]["domestic-rules"]["cidrs"] = ["1.0.1.0/24"]
    data["rulesets"]["domestic-rules"]["domains"] = ["domestic.example"]
    data["rulesets"]["domestic-rules"].pop("sha256", None)
    config = WayviaConfig.model_validate(data)
    return yaml.safe_load(
        ProfileRenderer(config).render({"id": "test"}, "PRIVATE-PASSWORD", "mihomo")
    )


def save(path, value):
    path.write_text(yaml.safe_dump(value, allow_unicode=True))
    path.chmod(0o600)


def test_effective_profile_and_route_override_diagnostics_are_redacted(profile, tmp_path):
    assert inspect(profile, profile)["status"] == "compatible"
    modified = copy.deepcopy(profile)
    modified["rules"].insert(0, "DOMAIN,SECRET.example,DIRECT")
    modified["dns"]["nameserver"] = ["https://SECRET.example/dns-query"]
    result = inspect(modified, profile)
    assert "CLIENT_ROUTE_OVERRIDE" in result["errors"] and "CLIENT_DNS_OVERRIDE" in result["errors"]
    assert "SECRET" not in json.dumps(result) and "PRIVATE" not in json.dumps(result)
    source, reference = tmp_path / "effective.yaml", tmp_path / "reference.yaml"
    save(source, modified)
    save(reference, profile)
    report = tmp_path / "private" / "report.json"
    run = CliRunner().invoke(
        app,
        ["profile", "inspect", str(source), "--reference", str(reference), "--output", str(report)],
    )
    assert run.exit_code == 5 and report.stat().st_mode & 0o777 == 0o600
    assert "SECRET" not in report.read_text() and "PRIVATE" not in report.read_text()


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda x: x.update(mode="direct"), "CLIENT_PROTECTED_DEFAULT"),
        (lambda x: x.update(**{"allow-lan": True}), "CLIENT_LOCAL_LISTENER"),
        (lambda x: x["proxy-groups"][0].update(proxies=["DIRECT", NODE]), "CLIENT_GROUP_POLICY"),
        (lambda x: x["proxy-groups"][0].update(**{"include-all": True}), "CLIENT_GROUP_POLICY"),
        (lambda x: x["proxies"][0].update(**{"skip-cert-verify": True}), "CLIENT_TLS_VERIFICATION"),
        (lambda x: x["tun"].update(**{"strict-route": False}), "CLIENT_TUN_GUARD"),
        (
            lambda x: x.update(**{"external-controller": "0.0.0.0:9090"}),
            "CLIENT_CONTROLLER_EXPOSURE",
        ),
    ],
)
def test_dangerous_client_overrides_are_detected(profile, mutation, code):
    candidate = copy.deepcopy(profile)
    mutation(candidate)
    assert code in inspect(candidate, profile)["errors"]


def test_proxy_only_mode_is_explicitly_partial_and_missing_reference_is_unknown(profile):
    candidate = copy.deepcopy(profile)
    candidate["tun"]["enable"] = False
    result = inspect(candidate)
    assert result["capture"] == "application_proxy_only"
    assert "CLIENT_SYSTEM_PROXY_PARTIAL_COVERAGE" in result["warnings"]
    assert not result["verification"]["reference_compared"]
    assert result["verification"]["actual_egress"] == "unknown"


def test_private_staging_retains_old_file_on_rejection_and_preserves_selection(profile, tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    dest = parent / "profile.yaml"
    save(dest, profile)
    before = dest.read_bytes()
    bad = copy.deepcopy(profile)
    bad["proxy-groups"][0]["proxies"] = ["DIRECT"]
    with pytest.raises(WayviaError) as exc:
        stage(bad, dest, profile)
    assert exc.value.code == "CLIENT_UPDATE_REJECTED" and dest.read_bytes() == before
    result = stage(profile, dest, profile, selected="Wayvia-entry")
    assert result["legacy_identity_migrated"] and result["selection_preserved"]
    assert result["selection"] == NODE and not result["client_reloaded"]
    assert dest.stat().st_mode & 0o777 == 0o600
    assert stage(profile, dest, profile, selected=NODE)["selection_preserved"]
    linked = parent / "linked.yaml"
    linked.symlink_to(dest)
    with pytest.raises(WayviaError):
        stage(profile, linked, profile)
    assert (
        dest.read_bytes() == yaml.safe_dump(profile, sort_keys=False, allow_unicode=True).encode()
    )


@pytest.mark.parametrize(
    "text", ["password: PRIVATE\npassword: OTHER\n", "[]", "dns: [", "!!python/object:invalid {}"]
)
def test_bad_or_public_profile_never_prints_input(text, tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(text)
    path.chmod(0o600)
    with pytest.raises(WayviaError) as exc:
        read_profile(path)
    assert "PRIVATE" not in str(exc.value) and "OTHER" not in str(exc.value)
    path.chmod(0o644)
    assert CliRunner().invoke(app, ["profile", "inspect", str(path)]).exit_code != 0


class Payload(httpx.SyncByteStream):
    def __init__(self, blocks):
        self.blocks = blocks

    def __iter__(self):
        yield from self.blocks


def clock():
    values = iter(n / 100 for n in range(100))
    return lambda: next(values)


def test_measurement_requires_explicit_action_and_uses_mock_targets_only(monkeypatch):
    calls = []

    def target(request):
        calls.append(request)
        return httpx.Response(204, stream=Payload([]))

    with pytest.raises(WayviaError):
        measure(transport=httpx.MockTransport(target))
    assert calls == []
    result = measure(authorized=True, transport=httpx.MockTransport(target), clock=clock())
    assert result["status"] == "measured" and result["latency_ms"] is not None
    assert result["download_mbps"] is None and result["https_available"] is True
    assert result["limits"]["full_server_path"] == "not_verified"
    assert len(calls) == 1 and calls[0].url.scheme == "https"
    assert "authorization" not in calls[0].headers


def test_throughput_caps_payload_and_separates_units():
    def target(request):
        return httpx.Response(200, stream=Payload([b"x" * 2048, b"y" * 2048]))

    result = measure(
        kind="throughput",
        authorized=True,
        transport=httpx.MockTransport(target),
        max_bytes=1024,
        clock=clock(),
    )
    assert result["download_bytes"] == 1024 and result["download_mbps"] > 0
    assert result["latency_ms"] is None and not result["response_complete"]
    assert result["error"] == "CLIENT_MEASUREMENT_BYTE_LIMIT"


@pytest.mark.parametrize(
    "status,encoding,code",
    [
        (302, "identity", "CLIENT_TARGET_STATUS"),
        (500, "identity", "CLIENT_TARGET_STATUS"),
        (200, "gzip", "CLIENT_UNEXPECTED_ENCODING"),
    ],
)
def test_redirects_errors_and_compressed_payload_never_become_speed(status, encoding, code):
    def target(request):
        return httpx.Response(
            status,
            headers={"content-encoding": encoding, "location": "https://SECRET.invalid/"},
            stream=Payload([]),
        )

    result = measure(
        kind="throughput", authorized=True, transport=httpx.MockTransport(target), clock=clock()
    )
    assert (
        result["status"] == "unavailable"
        and result["download_mbps"] is None
        and result["error"] == code
    )
    assert "SECRET" not in json.dumps(result)


def test_proxy_failure_is_unknown_not_business_outage():
    def target(request):
        raise httpx.ConnectError("PRIVATE-IP secret response", request=request)

    result = measure(authorized=True, transport=httpx.MockTransport(target), clock=clock())
    assert result["error"] == "CLIENT_PROXY_OR_TARGET_FAILED" and result["status"] == "unavailable"
    assert result["latency_ms"] is None and "PRIVATE" not in json.dumps(result)


def test_real_helper_has_global_deadline_and_fixed_loopback_proxy(monkeypatch):
    import wayvia.client.measure as module

    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired("private helper", kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", run)
    result = module.measure(authorized=True, seconds=1)
    assert result["error"] == "CLIENT_MEASUREMENT_DEADLINE"
    args, options = calls[0]
    assert args[-2:] == ["-m", "wayvia.client.measure"] and options["timeout"] == 1
    assert json.loads(options["input"])["proxy_port"] == 7890
    assert "shell" not in options


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("listeners", [{"type": "mixed", "port": 9999}], "CLIENT_UNREVIEWED_EXTENSIONS"),
        ("proxy-providers", {"hidden": {"type": "http"}}, "CLIENT_UNREVIEWED_EXTENSIONS"),
        (
            "tun",
            {"enable": True, "route-exclude-address": ["0.0.0.0/0"]},
            "CLIENT_TUN_ROUTE_OVERRIDE",
        ),
    ],
)
def test_topology_and_tun_exclusions_require_review(profile, field, value, code):
    modified = copy.deepcopy(profile)
    if field == "tun":
        modified["tun"].update(value)
    else:
        modified[field] = value
    assert code in inspect(modified, profile)["errors"]


def test_client_and_server_diagnostics_preserve_freshness_and_redaction(profile):
    from wayvia.client.diagnostics import diagnose
    from wayvia.telemetry.records import stamp

    now = 1793000000
    current = {
        "current": True,
        "role": "ingress",
        "state": "DEGRADED",
        "id": "b" * 32,
        "generation": "b" * 32,
        "revision": "a" * 64,
        "monitored_at": stamp(now - 1),
        "monitor_valid_until": stamp(now + 60),
        "blocked_reason": "SECRET TOKEN",
        "plan": {"password": "PRIVATE"},
    }
    alerts = {
        "generated_at": stamp(now),
        "alerts": [
            {
                "alert_id": "c" * 64,
                "incident_id": "d" * 32,
                "category": "path_health",
                "status": "open",
                "next_action": "SECRET PRIVATE",
                "evidence": [{"role": "ingress", "revision": "a" * 64, "generation": "b" * 32}],
            }
        ],
    }
    result = diagnose(inspect(profile, profile), {"dataplane": [current]}, alerts, now=now)
    assert result["server_observation"] == "failed" and result["client_connectivity"] == "unknown"
    assert len(result["alert_references"]) == 1 and result["automatic_actions"] == []
    assert "SECRET" not in json.dumps(result) and "PRIVATE" not in json.dumps(result)
    assert (
        diagnose(inspect(profile), [current], alerts, now=now + 200)["server_observation"]
        == "unknown"
    )
    assert not diagnose(inspect(profile), [current], alerts, now=now + 200)["alerts_report_fresh"]
    assert (
        diagnose(inspect(profile), [current, current], now=now)["server_observation"] == "unknown"
    )


def test_malformed_status_containers_cannot_escape_into_error_tracebacks(profile):
    from wayvia.client.diagnostics import diagnose
    from wayvia.telemetry.records import stamp

    now = 1793000000
    for category in ([], {}, "path_health"):
        result = diagnose(
            inspect(profile),
            [{"current": True, "role": []}],
            {"generated_at": stamp(now), "alerts": [{"category": category, "status": []}]},
            now=now,
        )
        assert result["server_observation"] == "unknown" and result["alert_references"] == []


def test_empty_success_response_does_not_claim_zero_bandwidth():
    result = measure(
        kind="throughput",
        authorized=True,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Payload([]))),
        clock=clock(),
    )
    assert result["error"] == "CLIENT_MEASUREMENT_EMPTY_PAYLOAD"
    assert result["status"] == "unavailable" and result["download_mbps"] is None
