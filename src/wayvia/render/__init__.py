"""Complete client profiles and role-isolated real data-plane artifacts."""

from __future__ import annotations

import ipaddress
import json
from typing import Any

import yaml

from wayvia.config import Resolver, WayviaConfig, split_endpoint
from wayvia.errors import WayviaError
from wayvia.policy import PolicyCompiler

supported_formats = ("sing-box", "mihomo")
SECRET_ROOT = "/etc/wayvia/secrets"


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _secret_path(ref: str, suffix: str) -> str:
    return f"{SECRET_ROOT}/{ref}.{suffix}"


def _dns_server(resolver: Resolver, tag: str, detour: str | None = None) -> dict:
    result = {
        "type": resolver.protocol,
        "tag": tag,
        "server": resolver.address,
        "server_port": resolver.port,
    }
    if detour:
        result["detour"] = detour
    if not _is_ip(resolver.address):
        result["domain_resolver"] = "bootstrap"
    if resolver.protocol in {"tls", "https"}:
        result["tls"] = {
            "enabled": True,
            "insecure": False,
            "server_name": resolver.server_name or resolver.address,
        }
    if resolver.protocol == "https":
        result["path"] = resolver.path
    return result


def _mihomo_resolver(resolver: Resolver, route: str) -> str:
    host = f"[{resolver.address}]" if ":" in resolver.address else resolver.address
    if resolver.protocol == "udp":
        endpoint = f"{host}:{resolver.port}"
    elif resolver.protocol == "https":
        endpoint = f"https://{host}:{resolver.port}{resolver.path}"
    else:
        endpoint = f"{resolver.protocol}://{host}:{resolver.port}"
    if resolver.server_name and resolver.server_name != resolver.address:
        # mihomo does not provide an independent DNS SNI override for all transports.
        raise WayviaError(
            "unsupported_dns_tls_name", "DNS TLS name override is unsupported by mihomo"
        )
    return f"{endpoint}#{route}"


def _route_match(rule: dict) -> dict:
    result = {}
    for key, target in (
        ("domains", "domain_suffix"),
        ("exact_domains", "domain"),
        ("cidrs", "ip_cidr"),
        ("port", "port"),
        ("network", "network"),
        ("ip_version", "ip_version"),
    ):
        if key in rule:
            result[target] = rule[key]
    return result


class ProfileRenderer:
    supported_formats = supported_formats
    format_capabilities = {
        "sing-box": {
            "core_version": "1.14.3",
            "format": "json",
            "tun": True,
            "tcp": True,
            "udp": True,
            "direct_ipv6": True,
            "proxy_ipv6": False,
            "mobile_import_verified": False,
        },
        "mihomo": {
            "core_version": "1.19.32",
            "format": "yaml",
            "tun": True,
            "tcp": True,
            "udp": True,
            "direct_ipv6": True,
            "proxy_ipv6": False,
            "mobile_import_verified": False,
        },
    }

    def __init__(self, config: WayviaConfig):
        self.config = config

    def render(self, device: dict, password: str, format: str) -> str:
        if format not in self.supported_formats:
            raise WayviaError(
                "unsupported_format",
                "Unsupported client profile format",
                details={"supported_formats": list(self.supported_formats)},
            )
        if not password or not isinstance(password, str):
            raise WayviaError("missing_device_credential", "A device credential is required")
        if not device.get("id"):
            raise WayviaError("invalid_device", "A device identifier is required")
        if device.get("status") in {"revoked", "disabled"}:
            raise WayviaError("device_revoked", "A revoked device cannot receive a profile")
        rendered = self._sing_box(password) if format == "sing-box" else self._mihomo(password)
        # The renderer never copies device/config dictionaries into the document.
        # Each protocol field is explicitly selected; server refs and carrier remain private.
        return (
            _json(rendered) if format == "sing-box" else yaml.safe_dump(rendered, sort_keys=False)
        )

    def _sing_box(self, password: str) -> dict:
        config = self.config
        policy = PolicyCompiler(config).compile()
        host, port = split_endpoint(config.entry.endpoint)
        dns_servers = []
        for tag, ref in policy["dns"].items():
            resolver = config.dns.resolvers[ref]
            if tag == "proxy" and config.policy.udp == "block" and resolver.protocol == "udp":
                resolver = resolver.model_copy(update={"protocol": "tcp"})
            dns_servers.append(_dns_server(resolver, tag, "proxy" if tag == "proxy" else "direct"))
        dns_rules = []
        for rule in policy["rules"]:
            if "domains" in rule or "exact_domains" in rule:
                match = _route_match(rule)
                match.pop("port", None)
                if rule["action"] == "block":
                    dns_rules.append({**match, "action": "reject"})
                else:
                    dns_rules.append({**match, "action": "route", "server": rule["dns"]})
        # Domestic rules above retain AAAA. Unknown/foreign AAAA is empty, allowing
        # applications to use IPv4; hard-coded foreign IPv6 is independently rejected.
        dns_rules.append({"query_type": ["AAAA"], "action": "predefined", "rcode": "NOERROR"})
        rules = [{"action": "sniff"}, {"protocol": "dns", "action": "hijack-dns"}]
        resolved = False
        for rule in policy["rules"]:
            if rule["id"] == "default":
                continue
            match = _route_match(rule)
            if "cidrs" in rule and not resolved:
                rules.append({"action": "resolve"})
                resolved = True
            action = (
                {"action": "reject"}
                if rule["action"] == "block"
                else {
                    "action": "route",
                    "outbound": (
                        "direct-" + rule["dns"]
                        if rule["dns"] in {"internal", "bootstrap"}
                        else rule["action"]
                    ),
                }
            )
            rules.append({**match, **action})
        return {
            "log": {"level": "warn", "timestamp": True},
            "dns": {
                "servers": dns_servers,
                "rules": dns_rules,
                "final": "proxy",
                "independent_cache": True,
            },
            "inbounds": [
                {
                    "type": "tun",
                    "tag": "wayvia-tun",
                    "interface_name": "wayvia-tun",
                    "address": ["172.28.0.1/30", "fd77:7761:7976::1/126"],
                    "mtu": config.vpn.mtu,
                    "auto_route": True,
                    "strict_route": True,
                    "stack": "mixed",
                }
            ],
            "outbounds": [
                {
                    "type": "trojan",
                    "tag": "proxy",
                    "server": host,
                    "server_port": port,
                    "password": password,
                    "domain_resolver": "bootstrap",
                    "tls": {
                        "enabled": True,
                        "insecure": False,
                        "server_name": config.entry.server_name,
                    },
                },
                {"type": "direct", "tag": "direct", "domain_resolver": "domestic"},
                {"type": "direct", "tag": "direct-internal", "domain_resolver": "internal"},
                {"type": "direct", "tag": "direct-bootstrap", "domain_resolver": "bootstrap"},
            ],
            "route": {
                "rules": rules,
                "final": "proxy",
                "auto_detect_interface": True,
                "default_domain_resolver": "proxy",
            },
        }

    def _mihomo(self, password: str) -> dict:
        config = self.config
        policy = PolicyCompiler(config).compile()
        host, port = split_endpoint(config.entry.endpoint)
        resolvers = {}
        for tag, ref in policy["dns"].items():
            resolver = config.dns.resolvers[ref]
            if tag == "proxy" and config.policy.udp == "block" and resolver.protocol == "udp":
                resolver = resolver.model_copy(update={"protocol": "tcp"})
            resolvers[tag] = _mihomo_resolver(resolver, "Wayvia" if tag == "proxy" else "DIRECT")
        nameserver_policy = {}
        # mihomo's domain trie chooses the most specific suffix. Our policy is
        # first-match: a later child of an earlier suffix must therefore be
        # omitted, while an earlier child remains alongside a later parent.
        # Looking up its label suffixes keeps this linear in the rule input.
        prior_suffixes = set()
        for rule in policy["rules"]:
            if rule["action"] != "direct":
                continue
            for domain in rule.get("domains", []):
                labels = domain.split(".")
                if any(".".join(labels[index:]) in prior_suffixes for index in range(len(labels))):
                    continue
                # '+.' inserts both exact and suffix nodes in mihomo. Preserve
                # an earlier bootstrap exact node with a subdomain-only suffix.
                key = f".{domain}" if domain in nameserver_policy else f"+.{domain}"
                nameserver_policy[key] = [resolvers[rule["dns"]]]
                prior_suffixes.add(domain)
            for domain in rule.get("exact_domains", []):
                nameserver_policy.setdefault(domain, [resolvers[rule["dns"]]])
        rules = []
        for rule in policy["rules"]:
            if rule["id"] == "default":
                continue
            outbound = "DIRECT" if rule["action"] == "direct" else "REJECT"
            if rule.get("exact_domains"):
                for domain in rule["exact_domains"]:
                    rules.append(f"AND,((DOMAIN,{domain}),(DST-PORT,{rule['port']})),{outbound}")
            for domain in rule.get("domains", []):
                rules.append(f"DOMAIN-SUFFIX,{domain},{outbound}")
            for cidr in rule.get("cidrs", []):
                kind = "IP-CIDR6" if ":" in cidr else "IP-CIDR"
                match = f"{kind},{cidr}"
                if rule.get("port"):
                    rules.append(f"AND,(({match}),(DST-PORT,{rule['port']})),{outbound}")
                else:
                    rules.append(f"{match},{outbound}")
            if rule.get("ip_version") == 6:
                rules.append("IP-CIDR6,::/0,REJECT,no-resolve")
            if rule.get("network") == "udp":
                rules.append("NETWORK,udp,REJECT")
        rules.append("MATCH,Wayvia")
        return {
            "mixed-port": 7890,
            "allow-lan": False,
            "mode": "rule",
            "log-level": "warning",
            "ipv6": True,
            "tun": {
                "enable": True,
                "stack": "mixed",
                "auto-route": True,
                "auto-detect-interface": True,
                "strict-route": True,
                "dns-hijack": ["any:53", "tcp://any:53"],
                "mtu": config.vpn.mtu,
            },
            "dns": {
                "enable": True,
                "listen": "127.0.0.1:1053",
                "ipv6": True,
                "enhanced-mode": "redir-host",
                "respect-rules": True,
                "use-hosts": False,
                "use-system-hosts": False,
                "default-nameserver": [resolvers["bootstrap"]],
                "proxy-server-nameserver": [resolvers["bootstrap"]],
                "direct-nameserver": [resolvers["domestic"]],
                "direct-nameserver-follow-policy": True,
                "nameserver": [resolvers["proxy"]],
                "nameserver-policy": nameserver_policy,
            },
            "proxies": [
                {
                    "name": "Wayvia-entry",
                    "type": "trojan",
                    "server": host,
                    "port": port,
                    "password": password,
                    "sni": config.entry.server_name,
                    "skip-cert-verify": False,
                    "udp": config.policy.udp == "allow",
                }
            ],
            "proxy-groups": [{"name": "Wayvia", "type": "select", "proxies": ["Wayvia-entry"]}],
            "rules": rules,
        }


class ServerRenderer:
    def __init__(self, config: WayviaConfig):
        self.config = config

    def render(self, role: str, devices: list[dict], secrets: dict[str, str]) -> dict[str, str]:
        if role not in {"ingress", "exit"}:
            raise WayviaError("invalid_role", "Role must be ingress or exit")
        config = self.config
        carrier_password = secrets.get(config.carrier.credential_ref)
        if not isinstance(carrier_password, str) or not carrier_password:
            raise WayviaError("missing_carrier_credential", "Carrier credential is required")
        if role == "ingress":
            files = self._ingress(devices, secrets, carrier_password)
            processes = {
                "ingress.json": "host",
                "carrier.json": "host",
                "bridge.json": "wayvia-egress",
                "vpn-client.conf": "wayvia-egress",
            }
            required = [
                config.entry.tls_certificate_ref + ".crt",
                config.entry.tls_certificate_ref + ".key",
                config.vpn.ca_ref + ".crt",
                config.vpn.client_certificate_ref + ".crt",
                config.vpn.client_certificate_ref + ".key",
            ]
        else:
            files = self._exit(carrier_password)
            processes = {"exit.json": "host", "vpn-server.conf": "host"}
            required = [
                config.carrier.tls_certificate_ref + ".crt",
                config.carrier.tls_certificate_ref + ".key",
                config.vpn.ca_ref + ".crt",
                config.vpn.server_certificate_ref + ".crt",
                config.vpn.server_certificate_ref + ".key",
            ]
        if config.vpn.tls_crypt_ref:
            required.append(config.vpn.tls_crypt_ref + ".key")
        files["manifest.json"] = _json(
            {
                "schema_version": 1,
                "role": role,
                "revision": config.revision,
                "files": sorted(files),
                "process_namespaces": processes,
                "required_secret_files": required,
                "vpn_interface": config.vpn.interface,
                "vpn_subnet": config.vpn.subnet,
                "namespace_link": "10.89.0.0/30",
                "ipv6_proxy_supported": False,
                "entry_server_name": config.entry.server_name,
                "carrier_server_name": config.carrier.server_name,
                "vpn_server_name": config.vpn.server_name,
            }
        )
        return files

    def _ingress(self, devices: list[dict], secrets: dict[str, str], carrier_password: str) -> dict:
        config = self.config
        _, entry_port = split_endpoint(config.entry.endpoint)
        carrier_host, carrier_port = split_endpoint(config.carrier.endpoint)
        vpn_host, vpn_port = split_endpoint(config.vpn.remote_on_exit)
        users = []
        for device in devices:
            if device.get("status") in {"revoked", "disabled"}:
                continue
            device_id = device.get("id")
            password = secrets.get(device_id) or device.get("password")
            if (
                not isinstance(device_id, str)
                or not device_id
                or not isinstance(password, str)
                or not password
            ):
                raise WayviaError("missing_device_credential", "A device is missing its credential")
            if password == carrier_password:
                raise WayviaError(
                    "credential_roles_overlap", "Device and carrier credentials must differ"
                )
            if any(user["name"] == device_id or user["password"] == password for user in users):
                raise WayviaError(
                    "duplicate_device_credential", "Device credentials must be independent"
                )
            users.append({"name": device_id, "password": password})
        ingress = {
            "log": {"level": "warn"},
            "inbounds": [
                {
                    "type": "trojan",
                    "tag": "entry",
                    "listen": config.entry.listen,
                    "listen_port": entry_port,
                    "users": users,
                    "tls": {
                        "enabled": True,
                        "certificate_path": _secret_path(config.entry.tls_certificate_ref, "crt"),
                        "key_path": _secret_path(config.entry.tls_certificate_ref, "key"),
                    },
                }
            ],
            "outbounds": [
                {
                    "type": "socks",
                    "tag": "vpn-business",
                    "server": "10.89.0.2",
                    "server_port": 1081,
                    "version": "5",
                }
            ],
            "route": {"rules": [{"ip_version": 6, "action": "reject"}], "final": "vpn-business"},
        }
        if config.policy.udp == "block":
            ingress["route"]["rules"].append({"network": "udp", "action": "reject"})
        bootstrap = config.dns.resolvers[config.dns.bootstrap_resolver_ref]
        if not _is_ip(carrier_host) and bootstrap.protocol not in {"udp", "tcp"}:
            raise WayviaError(
                "unsupported_carrier_bootstrap",
                "v2rayA carrier bootstrap requires numeric UDP or TCP DNS",
            )
        carrier = {
            "log": {"level": "warn"},
            "dns": {"servers": [_dns_server(bootstrap, "bootstrap")], "final": "bootstrap"},
            "inbounds": [
                {"type": "socks", "tag": "vpn-carrier", "listen": "10.89.0.1", "listen_port": 1080}
            ],
            "outbounds": [
                {
                    "type": "trojan",
                    "tag": "japan-carrier",
                    "server": carrier_host,
                    "server_port": carrier_port,
                    "password": carrier_password,
                    "network": "tcp",
                    "domain_resolver": "bootstrap",
                    "tls": {
                        "enabled": True,
                        "insecure": False,
                        "server_name": config.carrier.server_name,
                    },
                }
            ],
            "route": {
                "rules": [
                    {
                        "ip_cidr": [vpn_host + "/32"],
                        "port": vpn_port,
                        "network": "tcp",
                        "action": "route",
                        "outbound": "japan-carrier",
                    },
                    {"action": "reject"},
                ]
            },
        }
        exit_dns = config.dns.resolvers[config.dns.proxy_resolver_ref]
        namespace_dns = _dns_server(exit_dns, "exit")
        namespace_dns["bind_interface"] = config.vpn.interface
        if not _is_ip(exit_dns.address) or ipaddress.ip_address(exit_dns.address).version != 4:
            raise WayviaError(
                "unsupported_exit_dns", "Server VPN DNS requires a numeric IPv4 address"
            )
        bridge = {
            "log": {"level": "warn"},
            "dns": {"servers": [namespace_dns], "final": "exit"},
            "inbounds": [
                {"type": "socks", "tag": "business", "listen": "10.89.0.2", "listen_port": 1081}
            ],
            "outbounds": [
                {
                    "type": "direct",
                    "tag": "vpn",
                    "bind_interface": config.vpn.interface,
                    "domain_resolver": "exit",
                }
            ],
            "route": {"rules": [{"ip_version": 6, "action": "reject"}], "final": "vpn"},
        }
        lines = [
            "client",
            f"dev {config.vpn.interface}",
            "proto tcp-client",
            "socket-flags TCP_NODELAY",
            f"remote {vpn_host} {vpn_port}",
            "socks-proxy 10.89.0.1 1080",
            "nobind",
            "persist-key",
            "remote-cert-tls server",
            f"verify-x509-name {config.vpn.server_name} name",
            f"ca {_secret_path(config.vpn.ca_ref, 'crt')}",
            f"cert {_secret_path(config.vpn.client_certificate_ref, 'crt')}",
            f"key {_secret_path(config.vpn.client_certificate_ref, 'key')}",
            "data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305",
            "auth SHA256",
            "tls-version-min 1.2",
            "allow-compression no",
            "route-nopull",
            "redirect-gateway def1",
            "route 10.89.0.0 255.255.255.252 net_gateway",
            f"tun-mtu {config.vpn.mtu}",
            "connect-retry 2 30",
            "connect-timeout 10",
            "ping 10",
            "ping-restart 60",
            "verb 3",
        ]
        if config.vpn.tls_crypt_ref:
            lines.append(f"tls-crypt {_secret_path(config.vpn.tls_crypt_ref, 'key')}")
        return {
            "ingress.json": _json(ingress),
            "carrier.json": _json(carrier),
            "bridge.json": _json(bridge),
            "vpn-client.conf": "\n".join(lines) + "\n",
        }

    def _exit(self, carrier_password: str) -> dict:
        config = self.config
        _, carrier_port = split_endpoint(config.carrier.endpoint)
        vpn_host, vpn_port = split_endpoint(config.vpn.remote_on_exit)
        exit_config = {
            "log": {"level": "warn"},
            "inbounds": [
                {
                    "type": "trojan",
                    "tag": "carrier",
                    "listen": config.carrier.listen,
                    "listen_port": carrier_port,
                    "users": [{"name": "wayvia-carrier", "password": carrier_password}],
                    "tls": {
                        "enabled": True,
                        "certificate_path": _secret_path(config.carrier.tls_certificate_ref, "crt"),
                        "key_path": _secret_path(config.carrier.tls_certificate_ref, "key"),
                    },
                }
            ],
            "outbounds": [{"type": "direct", "tag": "vpn-only"}],
            "route": {
                "rules": [
                    {
                        "ip_cidr": [vpn_host + "/32"],
                        "port": vpn_port,
                        "network": "tcp",
                        "action": "route",
                        "outbound": "vpn-only",
                        "override_address": vpn_host,
                        "override_port": vpn_port,
                    },
                    {"action": "reject"},
                ]
            },
        }
        subnet = ipaddress.ip_network(config.vpn.subnet)
        lines = [
            f"local {vpn_host}",
            f"port {vpn_port}",
            "proto tcp-server",
            "socket-flags TCP_NODELAY",
            f"dev {config.vpn.interface}",
            "topology subnet",
            f"server {subnet.network_address} {subnet.netmask}",
            f"ca {_secret_path(config.vpn.ca_ref, 'crt')}",
            f"cert {_secret_path(config.vpn.server_certificate_ref, 'crt')}",
            f"key {_secret_path(config.vpn.server_certificate_ref, 'key')}",
            "dh none",
            "verify-client-cert require",
            "remote-cert-tls client",
            "data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305",
            "auth SHA256",
            "tls-version-min 1.2",
            "allow-compression no",
            "keepalive 10 60",
            "persist-key",
            "persist-tun",
            f"tun-mtu {config.vpn.mtu}",
            "verb 3",
        ]
        if config.vpn.tls_crypt_ref:
            lines.append(f"tls-crypt {_secret_path(config.vpn.tls_crypt_ref, 'key')}")
        return {"exit.json": _json(exit_config), "vpn-server.conf": "\n".join(lines) + "\n"}
