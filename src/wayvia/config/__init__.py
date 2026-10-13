"""Versioned, strict configuration and provenance-aware rule inputs."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from wayvia.errors import WayviaError

SecretRef = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
Port = Annotated[int, Field(ge=1, le=65535)]


def normalize_domain(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_domain")
    return _normalized_domain(value)


@lru_cache(maxsize=131072)
def _normalized_domain(value: str) -> str:
    value = value.rstrip(".").lower()
    if value.startswith("*."):
        value = value[2:]
    try:
        if not value.isascii():
            value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("invalid_domain") from exc
    if (
        len(value) > 253
        or not value
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in value.split(".")
        )
    ):
        raise ValueError("invalid_domain")
    return value


def split_endpoint(value: str) -> tuple[str, int]:
    """Accept DNS/IPv4 host:port and bracketed IPv6; reject URL/userinfo."""
    if value.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\]:(\d+)", value)
        if not match:
            raise ValueError("invalid_endpoint")
        host, port_text = match.groups()
        if ipaddress.ip_address(host).version != 6:
            raise ValueError("invalid_endpoint")
    else:
        if value.count(":") != 1:
            raise ValueError("invalid_endpoint")
        host, port_text = value.rsplit(":", 1)
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            host = normalize_domain(host)
    if not port_text.isdecimal() or not 1 <= int(port_text) <= 65535:
        raise ValueError("invalid_port")
    return host, int(port_text)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


class Deployment(StrictModel):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
    mode: Literal["production", "lab"] = "production"


class Entry(StrictModel):
    endpoint: str
    protocol: Literal["trojan"] = "trojan"
    tls_certificate_ref: SecretRef
    server_name: str
    listen: str = "0.0.0.0"

    @field_validator("endpoint")
    @classmethod
    def endpoint_valid(cls, value: str) -> str:
        split_endpoint(value)
        return value

    @field_validator("server_name")
    @classmethod
    def sni_valid(cls, value: str) -> str:
        return normalize_domain(value)

    @field_validator("listen")
    @classmethod
    def listen_valid(cls, value: str) -> str:
        return str(ipaddress.ip_address(value))


class Carrier(Entry):
    credential_ref: SecretRef


class VPN(StrictModel):
    transport: Literal["tcp"] = "tcp"
    remote_on_exit: str = "127.0.0.1:1194"
    client_certificate_ref: SecretRef
    server_certificate_ref: SecretRef
    ca_ref: SecretRef = "vpn-ca"
    server_name: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.-]{1,64}$")] = "wayvia-vpn-server"
    tls_crypt_ref: SecretRef | None = None
    subnet: str = "10.77.0.0/24"
    interface: Literal["tun0"] = "tun0"
    mtu: Annotated[int, Field(ge=1200, le=1500)] = 1400
    expected_exit_ip: str | None = None

    @field_validator("remote_on_exit")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        host, _ = split_endpoint(value)
        if host != "127.0.0.1":
            raise ValueError("vpn_endpoint_must_be_ipv4_loopback")
        return value

    @field_validator("subnet")
    @classmethod
    def subnet_valid(cls, value: str) -> str:
        subnet = ipaddress.ip_network(value, strict=True)
        if subnet.version != 4 or not subnet.is_private or not 16 <= subnet.prefixlen <= 29:
            raise ValueError("vpn_subnet_must_be_private_ipv4_slash16_to29")
        if subnet.overlaps(ipaddress.ip_network("10.89.0.0/30")):
            raise ValueError("vpn_subnet_conflicts_with_namespace_link")
        return str(subnet)

    @field_validator("expected_exit_ip")
    @classmethod
    def expected_ip_valid(cls, value: str | None) -> str | None:
        return str(ipaddress.ip_address(value)) if value else None


class RuleSet(StrictModel):
    source: Annotated[str, Field(min_length=1, max_length=512)]
    version: Annotated[str, Field(min_length=1, max_length=128)]
    domains: list[str] = Field(default_factory=list)
    cidrs: list[str] = Field(default_factory=list)
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None

    @field_validator("domains")
    @classmethod
    def domains_valid(cls, values: list[str]) -> list[str]:
        normalized = [normalize_domain(value) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate_domain_rule")
        return normalized

    @field_validator("cidrs")
    @classmethod
    def cidrs_valid(cls, values: list[str]) -> list[str]:
        normalized = [str(ipaddress.ip_network(value, strict=True)) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate_network_rule")
        return normalized

    @model_validator(mode="after")
    def verify_digest(self) -> RuleSet:
        if not self.domains and not self.cidrs:
            raise ValueError("empty_rule_set")
        if self.sha256 and self.sha256 != self.content_digest:
            raise ValueError("rule_set_digest_mismatch")
        return self

    @property
    def content_digest(self) -> str:
        body = json.dumps({"domains": self.domains, "cidrs": self.cidrs}, sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()


class ExplicitRule(StrictModel):
    id: SecretRef
    domain: str | None = None
    cidr: str | None = None
    action: Literal["direct", "block"]

    @field_validator("domain")
    @classmethod
    def domain_valid(cls, value: str | None) -> str | None:
        return normalize_domain(value) if value else None

    @field_validator("cidr")
    @classmethod
    def cidr_valid(cls, value: str | None) -> str | None:
        return str(ipaddress.ip_network(value, strict=True)) if value else None

    @model_validator(mode="after")
    def one_target(self) -> ExplicitRule:
        if bool(self.domain) == bool(self.cidr):
            raise ValueError("explicit_rule_requires_one_target")
        return self


class Policy(StrictModel):
    domestic_rules_ref: SecretRef
    domestic_domain_rules_ref: SecretRef | None = None
    local_rules_ref: SecretRef
    default_action: Literal["proxy"] = "proxy"
    proxy_ipv6: Literal["block_until_supported"] = "block_until_supported"
    udp: Literal["allow", "block"] = "allow"
    explicit_rules: list[ExplicitRule] = Field(default_factory=list)


class Resolver(StrictModel):
    address: str
    port: Port = 53
    protocol: Literal["udp", "tcp", "tls", "https"] = "udp"
    server_name: str | None = None
    path: str = "/dns-query"

    @field_validator("address")
    @classmethod
    def address_valid(cls, value: str) -> str:
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            return normalize_domain(value)

    @field_validator("server_name")
    @classmethod
    def name_valid(cls, value: str | None) -> str | None:
        return normalize_domain(value) if value else None

    @field_validator("path")
    @classmethod
    def path_valid(cls, value: str) -> str:
        if not re.fullmatch(r"/[a-zA-Z0-9/_.-]*", value):
            raise ValueError("invalid_dns_path")
        return value


class DNS(StrictModel):
    domestic_resolver_ref: SecretRef
    internal_resolver_ref: SecretRef
    proxy_resolver_ref: SecretRef
    bootstrap_resolver_ref: SecretRef
    resolvers: dict[SecretRef, Resolver]


class WayviaConfig(StrictModel):
    schema_version: Literal[1]
    deployment: Deployment
    entry: Entry
    carrier: Carrier
    vpn: VPN
    policy: Policy
    dns: DNS
    rulesets: dict[SecretRef, RuleSet]

    @model_validator(mode="after")
    def references_and_conflicts(self) -> WayviaConfig:
        for ref in (
            self.policy.domestic_rules_ref,
            self.policy.local_rules_ref,
            self.policy.domestic_domain_rules_ref,
        ):
            if ref is None:
                continue
            if ref not in self.rulesets:
                raise ValueError("missing_rule_set_reference")
        for ref in (
            self.dns.domestic_resolver_ref,
            self.dns.internal_resolver_ref,
            self.dns.proxy_resolver_ref,
            self.dns.bootstrap_resolver_ref,
        ):
            if ref not in self.dns.resolvers:
                raise ValueError("missing_dns_resolver_reference")
        bootstrap = self.dns.resolvers[self.dns.bootstrap_resolver_ref]
        try:
            ipaddress.ip_address(bootstrap.address)
        except ValueError as exc:
            raise ValueError("bootstrap_dns_requires_ip_address") from exc
        refs = [
            self.entry.tls_certificate_ref,
            self.carrier.credential_ref,
            self.carrier.tls_certificate_ref,
            self.vpn.client_certificate_ref,
            self.vpn.server_certificate_ref,
            self.vpn.ca_ref,
        ]
        if len(set(refs)) != len(refs):
            raise ValueError("credential_roles_must_use_distinct_references")
        if self.entry.endpoint == self.carrier.endpoint:
            raise ValueError("entry_and_carrier_endpoints_must_differ")
        direct_sets = [
            self.rulesets[self.policy.domestic_rules_ref],
            self.rulesets[self.policy.local_rules_ref],
        ]
        if self.policy.domestic_domain_rules_ref:
            direct_sets.append(self.rulesets[self.policy.domestic_domain_rules_ref])
        seen: set[str] = set()
        for rule in self.policy.explicit_rules:
            if rule.id in seen:
                raise ValueError("duplicate_explicit_rule_id")
            seen.add(rule.id)
            if rule.action == "block":
                for ruleset in direct_sets:
                    if rule.domain and any(
                        rule.domain == domain
                        or rule.domain.endswith("." + domain)
                        or domain.endswith("." + rule.domain)
                        for domain in ruleset.domains
                    ):
                        raise ValueError("block_rule_conflicts_with_direct_domain")
                    if rule.cidr and any(
                        ipaddress.ip_network(rule.cidr).version
                        == ipaddress.ip_network(cidr).version
                        and ipaddress.ip_network(rule.cidr).overlaps(ipaddress.ip_network(cidr))
                        for cidr in ruleset.cidrs
                    ):
                        raise ValueError("block_rule_conflicts_with_direct_network")
            for previous in self.policy.explicit_rules:
                if previous.id == rule.id:
                    break
                if previous.action == rule.action:
                    continue
                if (
                    previous.domain
                    and rule.domain
                    and (
                        previous.domain == rule.domain
                        or previous.domain.endswith("." + rule.domain)
                        or rule.domain.endswith("." + previous.domain)
                    )
                ):
                    raise ValueError("conflicting_explicit_domain_rules")
                if previous.cidr and rule.cidr:
                    first, second = (
                        ipaddress.ip_network(previous.cidr),
                        ipaddress.ip_network(rule.cidr),
                    )
                    if first.version == second.version and first.overlaps(second):
                        raise ValueError("conflicting_explicit_network_rules")
        return self

    @property
    def revision(self) -> str:
        body = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()


class _UniqueLoader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    pass


def _unique_mapping(loader: _UniqueLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError("duplicate_yaml_key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_config(path: str | Path) -> WayviaConfig:
    try:
        source = Path(path).read_text(encoding="utf-8")
        if len(source) > 8 * 1024 * 1024:
            raise ValueError("config_too_large")
        data = yaml.load(source, Loader=_UniqueLoader)
        return WayviaConfig.model_validate(data)
    except ValidationError as exc:
        # Pydantic inputs/context may contain passwords supplied to rejected fields.
        errors = [
            {
                "path": ".".join(map(str, error["loc"])),
                "code": error["type"],
                "message": error["msg"],
            }
            for error in exc.errors(include_input=False, include_context=False)
        ]
        raise WayviaError(
            "invalid_config", "Configuration validation failed", details={"errors": errors}
        ) from None
    except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        code = "config_not_readable" if isinstance(exc, OSError) else "invalid_yaml"
        raise WayviaError(code, "Configuration could not be read or parsed") from None


class ConfigLoader:
    load = staticmethod(load_config)


def example_config() -> dict:
    """A safe template: example addresses, no real credentials, explicit rule provenance."""
    domestic = json.loads(files(__package__).joinpath("data/cn-apnic.json").read_text())
    domestic_domains = json.loads(
        files(__package__).joinpath("data/domestic-domains.json").read_text()
    )
    return {
        "schema_version": 1,
        "deployment": {"id": "wayvia", "mode": "production"},
        "entry": {
            "endpoint": "entry.wayvia.example:443",
            "protocol": "trojan",
            "tls_certificate_ref": "entry-tls",
            "server_name": "entry.wayvia.example",
        },
        "carrier": {
            "endpoint": "exit.wayvia.example:443",
            "protocol": "trojan",
            "credential_ref": "carrier-credential",
            "tls_certificate_ref": "carrier-tls",
            "server_name": "exit.wayvia.example",
        },
        "vpn": {
            "transport": "tcp",
            "remote_on_exit": "127.0.0.1:1194",
            "client_certificate_ref": "vpn-client",
            "server_certificate_ref": "vpn-server",
            "ca_ref": "vpn-ca",
            "server_name": "wayvia-vpn-server",
            "subnet": "10.77.0.0/24",
            "interface": "tun0",
            "mtu": 1400,
        },
        "policy": {
            "domestic_rules_ref": "domestic-rules",
            "domestic_domain_rules_ref": "domestic-domains",
            "local_rules_ref": "local-rules",
            "default_action": "proxy",
            "proxy_ipv6": "block_until_supported",
            "udp": "allow",
        },
        "dns": {
            "domestic_resolver_ref": "domestic-dns",
            "internal_resolver_ref": "internal-dns",
            "proxy_resolver_ref": "exit-dns",
            "bootstrap_resolver_ref": "bootstrap-dns",
            "resolvers": {
                "domestic-dns": {"address": "223.5.5.5", "port": 53, "protocol": "udp"},
                "internal-dns": {"address": "192.168.1.1", "port": 53, "protocol": "udp"},
                "exit-dns": {"address": "1.1.1.1", "port": 53, "protocol": "udp"},
                "bootstrap-dns": {"address": "223.5.5.5", "port": 53, "protocol": "udp"},
            },
        },
        "rulesets": {
            "domestic-rules": domestic,
            "domestic-domains": domestic_domains,
            "local-rules": {
                "source": "IANA private-use and operator local domains",
                "version": "iana-special-use-1",
                "domains": ["localhost", "local"],
                "cidrs": [
                    "10.0.0.0/8",
                    "172.16.0.0/12",
                    "192.168.0.0/16",
                    "127.0.0.0/8",
                    "169.254.0.0/16",
                    "::1/128",
                    "fc00::/7",
                    "fe80::/10",
                ],
            },
        },
    }


class RuleSetImporter:
    """Import explicit local inputs, never silently fetch a mutable remote rule source."""

    @staticmethod
    def from_apnic(path: str | Path, *, expected_sha256: str, country: str = "CN") -> RuleSet:
        raw = Path(path).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected_sha256:
            raise WayviaError("rule_source_digest_mismatch", "Rule source digest does not match")
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise WayviaError("invalid_country", "Expected a two-letter allocation country")
        networks = []
        serial = None
        try:
            for line in raw.decode("utf-8").splitlines():
                if line.startswith("#") or not line:
                    continue
                parts = line.split("|")
                if parts[0] == "2" and parts[1] == "apnic":
                    serial = parts[2]
                if len(parts) < 7 or parts[0] != "apnic" or parts[1] != country:
                    continue
                if parts[6] not in {"allocated", "assigned"}:
                    continue
                if parts[2] == "ipv4":
                    first = ipaddress.IPv4Address(parts[3])
                    last = ipaddress.IPv4Address(int(first) + int(parts[4]) - 1)
                    networks.extend(ipaddress.summarize_address_range(first, last))
                elif parts[2] == "ipv6":
                    networks.append(ipaddress.IPv6Network(f"{parts[3]}/{parts[4]}", strict=True))
        except (ValueError, IndexError, UnicodeError):
            raise WayviaError("invalid_rule_source", "Invalid APNIC delegated statistics") from None
        if not serial or not networks:
            raise WayviaError("empty_rule_source", "No allocated networks found in APNIC source")
        collapsed = []
        for family in (4, 6):
            collapsed.extend(
                str(net)
                for net in ipaddress.collapse_addresses(
                    net for net in networks if net.version == family
                )
            )
        return RuleSet(
            source="https://ftp.apnic.net/stats/apnic/delegated-apnic-" + serial,
            version=serial,
            cidrs=collapsed,
            source_sha256=digest,
        )

    @staticmethod
    def from_domains(
        path: str | Path, *, source: str, version: str, expected_sha256: str
    ) -> RuleSet:
        raw = Path(path).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected_sha256:
            raise WayviaError("rule_source_digest_mismatch", "Rule source digest does not match")
        try:
            domains = [
                line.strip()
                for line in raw.decode("utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            ruleset = RuleSet(source=source, version=version, domains=domains, source_sha256=digest)
            return ruleset.model_copy(update={"sha256": ruleset.content_digest})
        except (ValueError, UnicodeError):
            raise WayviaError(
                "invalid_rule_source", "Expected one domain suffix per line"
            ) from None

    @staticmethod
    def from_dnsmasq(
        path: str | Path, *, source: str, version: str, expected_sha256: str
    ) -> RuleSet:
        """Import suffix intent only; never inherit the source's DNS upstreams.

        Supports explicit `server=/domain/address[#port]` records. Comments and
        duplicate/parent-covered suffixes are harmless. Other dnsmasq directives
        and catch-all records fail closed instead of silently changing routing.
        """
        raw = Path(path).read_bytes()
        raw_digest = hashlib.sha256(raw).hexdigest()
        if raw_digest != expected_sha256:
            raise WayviaError("rule_source_digest_mismatch", "Rule source digest does not match")
        try:
            domains = set()
            for line in raw.decode("utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                match = re.fullmatch(r"server=/([^/]+)/([^/\s]+)", line)
                if not match:
                    raise ValueError("unsupported_dnsmasq_directive")
                domain, upstream = match.groups()
                address, separator, port = upstream.partition("#")
                ipaddress.ip_address(address)
                if separator and (not port.isdecimal() or not 1 <= int(port) <= 65535):
                    raise ValueError("invalid_dnsmasq_upstream")
                domains.add(normalize_domain(domain))
            minimal = []
            for domain in sorted(domains):
                labels = domain.split(".")
                if not any(".".join(labels[index:]) in domains for index in range(1, len(labels))):
                    minimal.append(domain)
            ruleset = RuleSet(
                source=source, version=version, domains=minimal, source_sha256=raw_digest
            )
            return ruleset.model_copy(update={"sha256": ruleset.content_digest})
        except (ValueError, UnicodeError):
            raise WayviaError(
                "invalid_rule_source", "Unsupported or invalid dnsmasq domain source"
            ) from None
