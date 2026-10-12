import json
import subprocess

import httpx
import pytest

from wayvia.adapters.base import AdapterError, CommandResult, CommandRunner
from wayvia.adapters.openvpn import OpenVPNAdapter, check_safe_config
from wayvia.adapters.singbox import SingBoxAdapter
from wayvia.adapters.v2raya import V2rayAAdapter, new_management_password
from wayvia.agent.models import Action
from wayvia.agent.server import dispatch, peer_allowed
from wayvia.agent.validation import validate_server_artifact


class Runner:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        if self.error:
            raise self.error
        return self.result or CommandResult(0, "sing-box version 1.14.3\n")


def test_real_command_runner_nonzero_timeout_and_secret_redaction(monkeypatch):
    runner = CommandRunner()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "secret", "password"),
    )
    with pytest.raises(AdapterError) as err:
        runner.run(["sing-box", "check"])
    assert err.value.code == "COMMAND_FAILED"
    assert "password" not in json.dumps(err.value.as_dict())

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 1, output="secret")

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(AdapterError) as err:
        runner.run(["openvpn"])
    assert err.value.code == "COMMAND_TIMEOUT"


def test_singbox_calls_real_parser_and_rejects_version(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    runner = Runner()
    assert SingBoxAdapter(runner).validate(path)["validated"]
    assert runner.calls[-1] == ["sing-box", "check", "-c", str(path)]
    with pytest.raises(AdapterError) as err:
        SingBoxAdapter(Runner(CommandResult(0, "sing-box version 1.14.4"))).validate(path)
    assert err.value.code == "VERSION_MISMATCH"


@pytest.mark.parametrize(
    "text",
    [
        "up /tmp/script",
        "plugin /tmp/plugin",
        "config /etc/passwd",
        "ca /etc/passwd",
        "ca /etc/wayvia/secrets/../../passwd",
        "script-security 2",
    ],
)
def test_vpn_rejects_arbitrary_privileged_paths_or_scripts(text):
    with pytest.raises(AdapterError):
        check_safe_config(text)


def test_openvpn_tls_validation_does_not_claim_crypto_dry_run(tmp_path):
    path = tmp_path / "vpn.conf"
    path.write_text("client\ndev tun0\n")
    with pytest.raises(AdapterError) as err:
        OpenVPNAdapter(Runner(CommandResult(0, "OpenVPN 2.6.19"))).validate(path)
    assert err.value.code == "ISOLATION_REQUIRED"


def test_v2raya_http200_failure_is_rejected_and_version_core_match_required():
    client = httpx.Client(
        base_url="http://127.0.0.1:2017",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"code": "FAIL", "message": "secret", "errorCode": "INVALID_CONFIG"}
            )
        ),
    )
    with pytest.raises(AdapterError) as err:
        V2rayAAdapter(client=client).inspect()
    assert err.value.code == "V2RAYA_REQUEST_FAILED"
    assert "secret" not in json.dumps(err.value.as_dict())
    client = httpx.Client(
        base_url="http://127.0.0.1:2017",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={"code": "SUCCESS", "data": {"version": "2.5.10", "coreVersionValid": False}},
            )
        ),
    )
    with pytest.raises(AdapterError) as err:
        V2rayAAdapter(client=client).validate()
    assert err.value.code == "VERSION_MISMATCH"
    assert len(new_management_password()) == 32


def test_v2raya_management_endpoint_is_local():
    with pytest.raises(AdapterError):
        V2rayAAdapter("https://public.example")


def test_agent_closed_protocol_and_uid_auth(monkeypatch):
    with pytest.raises(AdapterError):
        dispatch(None, {"operation": "shell", "argv": ["rm", "-rf"]})
    with pytest.raises(AdapterError):
        dispatch(None, {"operation": "status", "path": "/etc/passwd"})
    import struct

    import wayvia.agent.server as module

    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.socket, "SO_PEERCRED", 17, raising=False)

    class Peer:
        def getsockopt(self, *args):
            return struct.pack("3i", 100, 2000, 3000)

    assert peer_allowed(Peer(), 2000)
    assert not peer_allowed(Peer(), 2001)


@pytest.mark.parametrize(
    "config",
    [
        {"inbounds": [{"type": "tun", "auto_route": True}], "outbounds": [{"type": "direct"}]},
        {"experimental": {"cache_file": {"path": "/etc/passwd"}}},
        {"log": {"output": "/etc/passwd"}},
        {"inbounds": [{"type": "trojan", "tls": {"certificate_path": "/etc/passwd"}}]},
    ],
)
def test_agent_rejects_raw_core_privilege_escape(config):
    with pytest.raises(AdapterError):
        validate_server_artifact("ingress.json", json.dumps(config))


def test_structured_action_rejects_nft_script_and_service_scope():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Action(kind="guard", resource="wayvia_guard", content="flush ruleset")
    with pytest.raises(ValidationError):
        Action(kind="service", resource="sshd.service")


def test_explicit_bootstrap_resolver_and_no_system_fallback():
    runner = Runner(CommandResult(0, "203.0.113.5\n"))
    adapter = V2rayAAdapter(runner=runner)
    node = {"server": "carrier.example.test", "domain_resolver": "bootstrap"}
    config = {
        "dns": {
            "servers": [
                {"tag": "bootstrap", "type": "udp", "server": "192.0.2.53", "server_port": 5353}
            ]
        }
    }
    assert adapter.resolve_carrier(node, config) == "203.0.113.5"
    assert runner.calls == [
        [
            "dig",
            "+short",
            "+tries=1",
            "+time=3",
            "@192.0.2.53",
            "-p",
            "5353",
            "carrier.example.test",
            "A",
        ]
    ]
    config["dns"]["servers"][0]["type"] = "https"
    with pytest.raises(AdapterError) as err:
        adapter.resolve_carrier(node, config)
    assert err.value.code == "BOOTSTRAP_DNS_UNSUPPORTED"
    assert len(runner.calls) == 1


def test_ip_carrier_endpoint_has_no_bootstrap_dependency():
    runner = Runner()
    assert (
        V2rayAAdapter(runner=runner).resolve_carrier({"server": "203.0.113.5"}, {}) == "203.0.113.5"
    )
    assert runner.calls == []
