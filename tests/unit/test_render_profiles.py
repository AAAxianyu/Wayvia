import json

import pytest
import yaml

from wayvia.config import WayviaConfig, example_config
from wayvia.errors import WayviaError
from wayvia.render import ProfileRenderer, ServerRenderer


@pytest.fixture
def config():
    return WayviaConfig.model_validate(example_config())


def test_safe_yaml_acceleration_and_fallback_preserve_profile(config, monkeypatch):
    renderer = ProfileRenderer(config)
    password = "false: '\n日本 !!python/object:example # [value]"
    expected = renderer._mihomo(password)
    fast = renderer.render({"id": "one"}, password, "mihomo")
    monkeypatch.delattr(yaml, "CSafeDumper", raising=False)
    fallback = renderer.render({"id": "one"}, password, "mihomo")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    assert yaml.load(fast, Loader=loader) == expected
    assert yaml.load(fallback, Loader=loader) == expected


@pytest.mark.parametrize("format", ProfileRenderer.supported_formats)
def test_complete_profile_has_only_terminal_credentials(config, format):
    device = {
        "id": "device-one",
        "status": "active",
        "platform": "android",
        "password": "ignored-device-field",
        "vpn_private_key": "ignored-private-key",
    }
    text = ProfileRenderer(config).render(device, "only-terminal-password", format)
    assert "only-terminal-password" in text
    for forbidden in (
        config.carrier.endpoint.split(":")[0],
        config.carrier.credential_ref,
        config.vpn.client_certificate_ref,
        config.vpn.server_certificate_ref,
        "ignored-device-field",
        "ignored-private-key",
    ):
        assert forbidden not in text
    profile = json.loads(text) if format == "sing-box" else yaml.safe_load(text)
    assert profile["dns"]
    if format == "sing-box":
        assert profile["inbounds"][0]["type"] == "tun"
        assert profile["inbounds"][0]["strict_route"]
        assert profile["route"]["final"] == "proxy"
        assert {"ip_version": 6, "action": "reject"} in profile["route"]["rules"]
        assert profile["outbounds"][0]["tls"]["insecure"] is False
        assert profile["dns"]["final"] == "proxy"
    else:
        assert profile["tun"]["enable"]
        assert profile["rules"][-1] == "MATCH,Wayvia"
        assert "IP-CIDR6,::/0,REJECT,no-resolve" in profile["rules"]
        assert profile["proxies"][0]["skip-cert-verify"] is False
        assert profile["dns"]["nameserver"][0].endswith("#Wayvia")
        assert "DIRECT" not in profile["proxy-groups"][0]["proxies"]


def test_sing_box_local_dns_and_unknown_aaaa_are_distinct(config):
    profile = json.loads(ProfileRenderer(config).render({"id": "one"}, "password", "sing-box"))
    dns = profile["dns"]
    servers = {server["tag"]: server for server in dns["servers"]}
    assert servers["internal"]["detour"] == "direct"
    assert servers["proxy"]["detour"] == "proxy"
    assert dns["rules"][0]["server"] == "bootstrap"
    assert dns["rules"][-1] == {"query_type": ["AAAA"], "action": "predefined", "rcode": "NOERROR"}
    rules = profile["route"]["rules"]
    local = next(rule for rule in rules if "local" in rule.get("domain_suffix", []))
    assert local["outbound"] == "direct-internal"
    assert rules.index(local) < next(
        i for i, rule in enumerate(rules) if rule.get("ip_version") == 6
    )


@pytest.mark.parametrize("format", ProfileRenderer.supported_formats)
def test_real_domestic_domains_select_direct_domestic_dns(config, format):
    text = ProfileRenderer(config).render({"id": "one"}, "password", format)
    profile = json.loads(text) if format == "sing-box" else yaml.safe_load(text)
    if format == "sing-box":
        domain_rule = next(
            rule
            for rule in profile["dns"]["rules"]
            if "bilibili.com" in rule.get("domain_suffix", [])
        )
        assert domain_rule["server"] == "domestic"
        routing = next(
            rule
            for rule in profile["route"]["rules"]
            if "bilibili.com" in rule.get("domain_suffix", [])
        )
        assert routing["outbound"] == "direct"
    else:
        assert "DOMAIN-SUFFIX,bilibili.com,DIRECT" in profile["rules"]
        assert profile["dns"]["nameserver-policy"]["+.bilibili.com"] == ["223.5.5.5:53#DIRECT"]


def test_revoked_device_and_unsupported_profile_format_are_errors(config):
    renderer = ProfileRenderer(config)
    with pytest.raises(WayviaError, match="revoked"):
        renderer.render({"id": "one", "status": "revoked"}, "password", "sing-box")
    with pytest.raises(WayviaError, match="Unsupported"):
        renderer.render({"id": "one"}, "password", "trojan-link")


@pytest.mark.parametrize("format", ProfileRenderer.supported_formats)
def test_udp_block_uses_tcp_for_proxy_dns(format):
    data = example_config()
    data["policy"]["udp"] = "block"
    text = ProfileRenderer(WayviaConfig.model_validate(data)).render(
        {"id": "one"}, "password", format
    )
    profile = json.loads(text) if format == "sing-box" else yaml.safe_load(text)
    if format == "sing-box":
        proxy_dns = next(server for server in profile["dns"]["servers"] if server["tag"] == "proxy")
        assert proxy_dns["type"] == "tcp"
        assert {"network": "udp", "action": "reject"} in profile["route"]["rules"]
    else:
        assert profile["dns"]["nameserver"][0].startswith("tcp://")
        assert "NETWORK,udp,REJECT" in profile["rules"]


def test_server_business_and_carrier_paths_are_separated(config):
    files = ServerRenderer(config).render(
        "ingress",
        [
            {"id": "one", "password": "device-one", "status": "active"},
            {"id": "two", "password": "device-two", "status": "revoked"},
        ],
        {"carrier-credential": "carrier-only", "vpn-secret": "private-key-never-embedded"},
    )
    ingress = json.loads(files["ingress.json"])
    carrier = json.loads(files["carrier.json"])
    bridge = json.loads(files["bridge.json"])
    manifest = json.loads(files["manifest.json"])
    assert ingress["outbounds"][0]["type"] == "socks"
    assert ingress["outbounds"][0]["server"] == "10.89.0.2"
    assert ingress["inbounds"][0]["users"] == [{"name": "one", "password": "device-one"}]
    assert "device-two" not in "".join(files.values())
    assert "carrier-only" not in files["ingress.json"]
    assert carrier["inbounds"][0]["listen"] == "10.89.0.1"
    assert carrier["route"]["rules"][-1]["action"] == "reject"
    assert bridge["outbounds"][0]["bind_interface"] == "tun0"
    assert bridge["dns"]["servers"][0]["bind_interface"] == "tun0"
    assert "socks-proxy 10.89.0.1 1080" in files["vpn-client.conf"]
    assert "verify-x509-name wayvia-vpn-server name" in files["vpn-client.conf"]
    assert "exit.wayvia.example" not in files["vpn-client.conf"]
    assert manifest["process_namespaces"]["ingress.json"] == "host"
    assert manifest["process_namespaces"]["vpn-client.conf"] == "wayvia-egress"
    assert "private-key-never-embedded" not in "".join(files.values())


def test_exit_cannot_proxy_arbitrary_destinations_and_has_no_device_password(config):
    files = ServerRenderer(config).render(
        "exit",
        [{"id": "one", "password": "terminal-secret"}],
        {"carrier-credential": "carrier-password"},
    )
    proxy = json.loads(files["exit.json"])
    assert proxy["route"]["rules"][0] == {
        "ip_cidr": ["127.0.0.1/32"],
        "port": 1194,
        "network": "tcp",
        "action": "route",
        "outbound": "vpn-only",
        "override_address": "127.0.0.1",
        "override_port": 1194,
    }
    assert proxy["route"]["rules"][-1] == {"action": "reject"}
    assert "local 127.0.0.1" in files["vpn-server.conf"]
    assert "verify-client-cert require" in files["vpn-server.conf"]
    assert "terminal-secret" not in "".join(files.values())
    assert "entry-tls" not in "".join(files.values())


def test_device_credentials_are_unique_and_missing_credentials_fail(config):
    renderer = ServerRenderer(config)
    carrier = {"carrier-credential": "carrier-password"}
    with pytest.raises(WayviaError, match="independent"):
        renderer.render(
            "ingress",
            [{"id": "one", "password": "same"}, {"id": "two", "password": "same"}],
            carrier,
        )
    with pytest.raises(WayviaError, match="missing"):
        renderer.render("ingress", [{"id": "one"}], carrier)
    with pytest.raises(WayviaError, match="Carrier"):
        renderer.render("ingress", [], {})
    with pytest.raises(WayviaError, match="differ"):
        renderer.render("ingress", [{"id": "one", "password": "carrier-password"}], carrier)


def test_server_render_rejects_bootstrap_dns_unsupported_by_locked_v2raya():
    data = example_config()
    data["dns"]["resolvers"]["bootstrap-dns"].update(protocol="tls", port=853)
    renderer = ServerRenderer(WayviaConfig.model_validate(data))
    with pytest.raises(WayviaError) as failure:
        renderer.render("ingress", [], {"carrier-credential": "carrier-password"})
    assert failure.value.code == "unsupported_carrier_bootstrap"


def test_server_render_rejects_ipv6_dns_when_vpn_ipv6_is_blocked():
    data = example_config()
    data["dns"]["resolvers"]["exit-dns"]["address"] = "2606:4700:4700::1111"
    renderer = ServerRenderer(WayviaConfig.model_validate(data))
    with pytest.raises(WayviaError) as failure:
        renderer.render("ingress", [], {"carrier-credential": "carrier-password"})
    assert failure.value.code == "unsupported_exit_dns"


@pytest.mark.parametrize(
    "local,domestic,expected",
    [
        (["campus.example"], ["campus.example"], {"+.campus.example": "internal"}),
        (["campus.example"], ["api.campus.example"], {"+.campus.example": "internal"}),
        (
            ["api.campus.example"],
            ["campus.example"],
            {"+.api.campus.example": "internal", "+.campus.example": "domestic"},
        ),
    ],
)
def test_mihomo_dns_preserves_first_match_across_identical_parent_and_child_suffixes(
    local, domestic, expected
):
    data = example_config()
    data["rulesets"]["local-rules"] = {"source": "operator", "version": "1", "domains": local}
    data["rulesets"]["domestic-rules"] = {"source": "operator", "version": "1", "domains": domestic}
    data["policy"]["domestic_domain_rules_ref"] = None
    renderer = ProfileRenderer(WayviaConfig.model_validate(data))
    policy = renderer._mihomo("test-password")["dns"]["nameserver-policy"]
    policy.pop("entry.wayvia.example")
    addresses = {"internal": ["192.168.1.1:53#DIRECT"], "domestic": ["223.5.5.5:53#DIRECT"]}
    assert policy == {key: addresses[value] for key, value in expected.items()}


def test_mihomo_bootstrap_exact_rule_does_not_get_overwritten_by_suffix_node():
    data = example_config()
    data["dns"]["resolvers"]["bootstrap-dns"]["address"] = "9.9.9.9"
    data["rulesets"]["domestic-rules"] = {
        "source": "operator",
        "version": "1",
        "domains": ["entry.wayvia.example"],
    }
    data["policy"]["domestic_domain_rules_ref"] = None
    policy = ProfileRenderer(WayviaConfig.model_validate(data))._mihomo("password")["dns"][
        "nameserver-policy"
    ]
    assert policy["entry.wayvia.example"] == ["9.9.9.9:53#DIRECT"]
    assert policy[".entry.wayvia.example"] == ["223.5.5.5:53#DIRECT"]
    assert "+.entry.wayvia.example" not in policy
