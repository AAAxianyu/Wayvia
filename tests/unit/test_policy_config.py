import copy
import hashlib
import json

import pytest
import yaml
from pydantic import ValidationError

from wayvia.config import RuleSetImporter, WayviaConfig, example_config, load_config
from wayvia.errors import WayviaError
from wayvia.policy import PolicyCompiler


@pytest.fixture
def config():
    return WayviaConfig.model_validate(example_config())


def test_bundled_rule_source_is_real_pinned_and_contains_both_ip_families(config):
    rules = config.rulesets[config.policy.domestic_rules_ref]
    assert len(rules.cidrs) > 7000
    assert rules.source.startswith("https://ftp.apnic.net/stats/apnic/delegated-apnic-")
    assert len(rules.source_sha256) == 64
    assert rules.sha256 == rules.content_digest
    assert any(":" in cidr for cidr in rules.cidrs)
    assert PolicyCompiler(config).explain(ip="223.5.5.5")["action"] == "direct"
    assert PolicyCompiler(config).explain(ip="2400:3200::1")["action"] == "direct"


@pytest.mark.parametrize(
    "domain,ip,action,rule",
    [
        ("www.baidu.com", None, "direct", "domestic-domain"),
        ("BAIDU.COM.", "8.8.8.8", "direct", "domestic-domain"),
        ("evilbaidu.com", None, "proxy", "default"),
        ("printer.local", None, "direct", "local-domain"),
        (None, "192.168.3.2", "direct", "local-ip"),
        (None, "fd00::123", "direct", "local-ip"),
        (None, "2606:4700::1111", "block", "proxy-ipv6-guard"),
        ("www.google.com", None, "proxy", "default"),
        (None, "8.8.8.8", "proxy", "default"),
    ],
)
def test_ordered_domain_and_ip_policy(config, domain, ip, action, rule):
    result = PolicyCompiler(config).explain(domain=domain, ip=ip)
    assert result["action"] == action
    assert result["rule"] == rule
    assert result["revision"] == config.revision
    assert result["rule_version"]


def test_entry_bypass_requires_exact_domain_and_endpoint_port(config):
    compiler = PolicyCompiler(config)
    assert compiler.explain(domain="entry.wayvia.example", port=443)["rule"] == "entry-bootstrap"
    assert compiler.explain(domain="entry.wayvia.example", port=444)["action"] == "proxy"
    assert compiler.explain(domain="evil.entry.wayvia.example")["action"] == "proxy"


def test_udp_block_keeps_domestic_udp_direct():
    data = example_config()
    data["policy"]["udp"] = "block"
    compiler = PolicyCompiler(WayviaConfig.model_validate(data))
    assert compiler.explain(domain="www.baidu.com", protocol="udp")["action"] == "direct"
    assert compiler.explain(domain="example.org", protocol="udp")["action"] == "block"
    assert compiler.explain(domain="example.org", protocol="tcp")["action"] == "proxy"


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"domain": "https://example.org"},
        {"ip": "999.1.1.1"},
        {"domain": "example.org", "protocol": "icmp"},
        {"domain": "example.org", "port": 0},
    ],
)
def test_invalid_policy_queries_are_errors(config, query):
    with pytest.raises(WayviaError, match="required|Invalid|Expected"):
        PolicyCompiler(config).explain(**query)


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("entry", "password", "secret-must-not-be-accepted"),
        ("vpn", "transport", "udp"),
        ("vpn", "remote_on_exit", "192.0.2.1:1194"),
        ("vpn", "subnet", "10.89.0.0/24"),
        ("policy", "proxy_ipv6", "enabled"),
        ("policy", "default_action", "direct"),
        ("carrier", "credential_ref", "../../etc/shadow"),
    ],
)
def test_unsupported_combinations_and_unknown_fields_rejected(section, key, value):
    data = example_config()
    data[section][key] = value
    with pytest.raises(ValidationError):
        WayviaConfig.model_validate(data)


def test_load_errors_have_field_path_without_rejected_sensitive_value(tmp_path):
    data = example_config()
    data["entry"]["password"] = "do-not-expose-this-value"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(WayviaError) as failure:
        load_config(path)
    serialized = json.dumps(failure.value.as_dict())
    assert "entry.password" in serialized
    assert "do-not-expose-this-value" not in serialized


def test_duplicate_yaml_key_is_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("schema_version: 1\nschema_version: 1\n")
    with pytest.raises(WayviaError) as failure:
        load_config(path)
    assert failure.value.code == "invalid_yaml"


def test_complex_yaml_mapping_key_is_rejected_with_structured_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("? [a, b]\n: value\n")
    with pytest.raises(WayviaError) as failure:
        load_config(path)
    assert failure.value.code == "invalid_yaml"


@pytest.mark.parametrize(
    "rule",
    [
        {"id": "bad", "domain": "baidu.com", "action": "block"},
        {"id": "bad", "cidr": "192.168.4.0/24", "action": "block"},
        {"id": "bad", "domain": "cn", "action": "proxy"},
    ],
)
def test_rules_cannot_override_domestic_or_local_direct(rule):
    data = example_config()
    data["policy"]["explicit_rules"] = [rule]
    with pytest.raises(ValidationError):
        WayviaConfig.model_validate(data)


def test_conflicting_explicit_networks_are_rejected():
    data = example_config()
    data["policy"]["explicit_rules"] = [
        {"id": "allow", "cidr": "198.18.0.0/16", "action": "direct"},
        {"id": "deny", "cidr": "198.18.2.0/24", "action": "block"},
    ]
    with pytest.raises(ValidationError, match="conflicting_explicit_network_rules"):
        WayviaConfig.model_validate(data)


def test_rule_digest_detects_changed_content():
    data = example_config()
    data["rulesets"]["domestic-rules"]["cidrs"].append("198.18.0.0/24")
    with pytest.raises(ValidationError, match="rule_set_digest_mismatch"):
        WayviaConfig.model_validate(data)


def test_same_credentials_cannot_be_reused_between_roles():
    data = example_config()
    data["carrier"]["credential_ref"] = data["entry"]["tls_certificate_ref"]
    with pytest.raises(ValidationError, match="credential_roles"):
        WayviaConfig.model_validate(data)


def test_import_apnic_converts_non_power_of_two_ranges_and_excludes_other_country(tmp_path):
    raw = (
        b"2|apnic|20261012|3|19830613|20261011|+1000\n"
        b"apnic|CN|ipv4|1.0.1.0|768|20100101|allocated\n"
        b"apnic|CN|ipv6|2400:3200::|32|20100101|allocated\n"
        b"apnic|JP|ipv4|1.0.4.0|1024|20100101|allocated\n"
    )
    path = tmp_path / "delegated.txt"
    path.write_bytes(raw)
    rules = RuleSetImporter.from_apnic(path, expected_sha256=hashlib.sha256(raw).hexdigest())
    assert rules.cidrs == ["1.0.1.0/24", "1.0.2.0/23", "2400:3200::/32"]
    assert rules.version == "20261012"
    with pytest.raises(WayviaError, match="digest"):
        RuleSetImporter.from_apnic(path, expected_sha256="0" * 64)


def test_import_domains_normalizes_unicode_and_checks_pinned_digest(tmp_path):
    raw = "# operator maintained\n中国.cn\nEXAMPLE.COM.\n".encode()
    path = tmp_path / "domains.txt"
    path.write_bytes(raw)
    rule = RuleSetImporter.from_domains(
        path, source="operator", version="1", expected_sha256=hashlib.sha256(raw).hexdigest()
    )
    assert rule.domains == ["xn--fiqs8s.cn", "example.com"]
    assert rule.sha256 == rule.content_digest
    copied = copy.deepcopy(rule.model_dump())
    copied["domains"].append("example.net")
    with pytest.raises(ValidationError):
        type(rule).model_validate(copied)


def test_domestic_domain_library_has_source_and_explains_bilibili_without_dumping_library(config):
    library = config.rulesets[config.policy.domestic_domain_rules_ref]
    assert len(library.domains) == 110185
    assert (
        library.source_sha256 == "c030567d770eb94e8fd56e75392ebb10ff5d4c3cf6e79d903c546f575a0145e3"
    )
    assert library.sha256 == library.content_digest
    result = PolicyCompiler(config).explain(domain="www.bilibili.com")
    assert result["action"] == "direct"
    assert result["dns"] == "domestic"
    assert result["rule"] == "domestic-library-domain"
    assert "domains" not in result["matched_rule"]
    assert len(json.dumps(result)) < 2000


def test_import_dnsmasq_has_strict_digest_ignores_disabled_and_deduplicates_suffixes(tmp_path):
    raw = (
        (
            b"#server=/disabled.example/114.114.114.114\n"
            b"server=/EXAMPLE.CN/114.114.114.114\n"
            b"server=/child.example.cn/8.8.8.8#5353\n"
            b"server=/example.cn/1.1.1.1\n"
        )
        .decode()
        .encode()
    )
    path = tmp_path / "domains.conf"
    path.write_bytes(raw)
    imported = RuleSetImporter.from_dnsmasq(
        path, source="operator-source", version="1", expected_sha256=hashlib.sha256(raw).hexdigest()
    )
    assert imported.domains == ["example.cn"]
    assert imported.sha256 == imported.content_digest
    assert "114.114.114.114" not in imported.model_dump_json()
    with pytest.raises(WayviaError, match="digest"):
        RuleSetImporter.from_dnsmasq(
            path, source="operator-source", version="1", expected_sha256="0" * 64
        )


@pytest.mark.parametrize(
    "line",
    [
        "address=/example.cn/1.1.1.1",
        "server=//1.1.1.1",
        "server=/a/b/1.1.1.1",
        "server=/example.cn/not-an-IP",
        "server=/example.cn/1.1.1.1#",
    ],
)
def test_import_dnsmasq_rejects_unsupported_records_without_partial_rules(tmp_path, line):
    raw = ("server=/valid.cn/1.1.1.1\n" + line + "\n").encode()
    path = tmp_path / "domains.conf"
    path.write_bytes(raw)
    with pytest.raises(WayviaError, match="Unsupported"):
        RuleSetImporter.from_dnsmasq(
            path,
            source="operator-source",
            version="1",
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )
