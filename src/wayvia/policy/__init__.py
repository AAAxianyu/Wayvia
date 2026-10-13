"""One ordered policy for explainers and both supported client formats."""

from __future__ import annotations

import ipaddress

from wayvia.config import WayviaConfig, normalize_domain, split_endpoint
from wayvia.errors import WayviaError


class PolicyCompiler:
    def __init__(self, config: WayviaConfig):
        self.config = config

    def compile(self) -> dict:
        config = self.config
        revision = config.revision
        host, port = split_endpoint(config.entry.endpoint)
        try:
            endpoint_ip = ipaddress.ip_address(host)
        except ValueError:
            endpoint_ip = None
        endpoint_match = (
            {"cidrs": [f"{host}/{endpoint_ip.max_prefixlen}"]}
            if endpoint_ip
            else {"exact_domains": [host]}
        )
        rules = [
            {
                "id": "entry-bootstrap",
                "action": "direct",
                "dns": "bootstrap",
                "source": "entry",
                "version": config.schema_version,
                "port": port,
                **endpoint_match,
            }
        ]
        rule_sources = [
            ("local", config.policy.local_rules_ref, "internal"),
            ("domestic", config.policy.domestic_rules_ref, "domestic"),
        ]
        if config.policy.domestic_domain_rules_ref:
            rule_sources.append(
                ("domestic-library", config.policy.domestic_domain_rules_ref, "domestic")
            )
        for name, ref, dns in rule_sources:
            ruleset = config.rulesets[ref]
            metadata = {
                "action": "direct",
                "source": ruleset.source,
                "version": ruleset.version,
                "digest": ruleset.content_digest,
                "dns": dns,
            }
            if ruleset.domains:
                rules.append({"id": f"{name}-domain", "domains": ruleset.domains, **metadata})
            if ruleset.cidrs:
                rules.append({"id": f"{name}-ip", "cidrs": ruleset.cidrs, **metadata})
        for rule in config.policy.explicit_rules:
            target = {"domains": [rule.domain]} if rule.domain else {"cidrs": [rule.cidr]}
            rules.append(
                {
                    "id": rule.id,
                    "action": rule.action,
                    "source": "explicit",
                    "version": revision,
                    "dns": "domestic" if rule.action == "direct" else "reject",
                    **target,
                }
            )
        rules.append(
            {
                "id": "proxy-ipv6-guard",
                "ip_version": 6,
                "action": "block",
                "source": "policy.proxy_ipv6",
                "version": revision,
                "dns": "proxy",
            }
        )
        if config.policy.udp == "block":
            rules.append(
                {
                    "id": "proxy-udp-guard",
                    "network": "udp",
                    "action": "block",
                    "source": "policy.udp",
                    "version": revision,
                    "dns": "proxy",
                }
            )
        rules.append(
            {
                "id": "default",
                "action": "proxy",
                "source": "policy.default_action",
                "version": revision,
                "dns": "proxy",
            }
        )
        return {
            "schema_version": 1,
            "revision": revision,
            "rules": rules,
            "dns": {
                "bootstrap": config.dns.bootstrap_resolver_ref,
                "internal": config.dns.internal_resolver_ref,
                "domestic": config.dns.domestic_resolver_ref,
                "proxy": config.dns.proxy_resolver_ref,
            },
            "capabilities": {
                "tcp": True,
                "udp": config.policy.udp == "allow",
                "proxy_ipv6": False,
                "direct_ipv6": True,
            },
        }

    def explain(self, domain=None, ip=None, protocol="tcp", port=443) -> dict:
        if protocol not in {"tcp", "udp"} or not isinstance(port, int) or not 1 <= port <= 65535:
            raise WayviaError("invalid_policy_query", "Expected TCP/UDP and a valid port")
        try:
            domain = normalize_domain(domain) if domain else None
            address = ipaddress.ip_address(ip) if ip else None
        except ValueError:
            raise WayviaError("invalid_policy_query", "Invalid domain or IP address") from None
        if not domain and not address:
            raise WayviaError("invalid_policy_query", "A domain or IP address is required")
        compiled = self.compile()
        for rule in compiled["rules"]:
            if rule.get("port") and rule["port"] != port:
                continue
            if rule.get("network") and rule["network"] != protocol:
                continue
            if rule.get("ip_version") and (not address or address.version != rule["ip_version"]):
                continue
            target_keys = {"domains", "exact_domains", "cidrs"} & rule.keys()
            if target_keys:
                domain_match = domain and (
                    domain in rule.get("exact_domains", [])
                    or any(
                        domain == suffix or domain.endswith("." + suffix)
                        for suffix in rule.get("domains", [])
                    )
                )
                ip_match = address and any(
                    address.version == ipaddress.ip_network(cidr).version
                    and address in ipaddress.ip_network(cidr)
                    for cidr in rule.get("cidrs", [])
                )
                if not domain_match and not ip_match:
                    continue
            return {
                "action": rule["action"],
                "rule": rule["id"],
                "matched_rule": {
                    key: value
                    for key, value in rule.items()
                    if key not in {"domains", "exact_domains", "cidrs"}
                },
                "revision": compiled["revision"],
                "rule_version": rule["version"],
                "dns": rule["dns"],
                "query": {
                    "domain": domain,
                    "ip": str(address) if address else None,
                    "protocol": protocol,
                    "port": port,
                },
            }
        raise AssertionError("compiled policy must contain a default rule")


PolicyExplainer = PolicyCompiler
