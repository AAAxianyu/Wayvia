"""Enforce the fixed data-plane topology before a privileged file is installed."""

import ipaddress
import json
import re
import shlex
from pathlib import Path

from wayvia.adapters.base import AdapterError
from wayvia.adapters.openvpn import check_safe_config

CORE_KEYS = set(
    "log level inbounds outbounds dns route type tag listen listen_port users name "
    "password tls enabled certificate_path key_path server server_port version rules "
    "final ip_version action network ip_cidr port outbound override_address override_port "
    "domain_resolver insecure server_name bind_interface servers path detour".split()
)


def fail():
    raise AdapterError("TOPOLOGY_FORBIDDEN", "Configuration violates the fixed Wayvia topology")


def secret_file(value, *, must_exist=False):
    if not isinstance(value, str) or not re.fullmatch(
        r"/etc/wayvia/secrets/[a-zA-Z0-9_-]+\.(crt|key)", value
    ):
        raise AdapterError("SECRET_PATH_FORBIDDEN", "Only named Wayvia secret files may be read")
    path = Path(value)
    if must_exist and any(parent.is_symlink() for parent in (path, *path.parents)):
        raise AdapterError("SECRET_PATH_FORBIDDEN", "Secret paths must not traverse symlinks")
    if must_exist and not path.is_file():
        raise AdapterError("SECRET_MISSING", "Required Wayvia secret material is absent")
    if path.exists() and (path.stat().st_uid != 0 or path.stat().st_mode & 0o077):
        raise AdapterError(
            "SECRET_PERMISSIONS", "Server secrets require root ownership and owner-only permissions"
        )


def validate_server_artifact(name, text, *, must_exist=False):
    if name == "manifest.json":
        json.loads(text)
        return
    if name.endswith(".conf"):
        check_safe_config(text)
        options = {}
        for line in text.splitlines():
            words = shlex.split(line, comments=True)
            if words:
                options.setdefault(words[0], []).append(words[1:])
        if options.get("dev") != [["tun0"]] or options.get("tls-version-min") != [["1.2"]]:
            fail()
        for option in ("ca", "cert", "key", "tls-crypt", "tls-auth"):
            for words in options.get(option, []):
                secret_file(words[0], must_exist=must_exist)
        if any(
            key in options
            for key in (
                "ifconfig-pool-persist",
                "push",
                "route-ipv6",
                "server-ipv6",
                "script-security",
            )
        ):
            fail()
        if name == "vpn-client.conf":
            if (
                options.get("proto") != [["tcp-client"]]
                or options.get("remote") != [["127.0.0.1", "1194"]]
                or options.get("socks-proxy") != [["10.89.0.1", "1080"]]
                or options.get("remote-cert-tls") != [["server"]]
                or "route-nopull" not in options
                or "verify-x509-name" not in options
            ):
                fail()
        elif name == "vpn-server.conf":
            if (
                options.get("proto") != [["tcp-server"]]
                or options.get("local") != [["127.0.0.1"]]
                or options.get("port") != [["1194"]]
                or options.get("verify-client-cert") != [["require"]]
                or any(
                    key in options
                    for key in ("route", "redirect-gateway", "remote", "client", "socks-proxy")
                )
            ):
                fail()
        return
    config = json.loads(text)

    def walk(obj, trail=()):
        if isinstance(obj, dict):
            if set(obj) - CORE_KEYS:
                fail()
            for key, value in obj.items():
                if key in {"certificate_path", "key_path"}:
                    secret_file(value, must_exist=must_exist)
                if key == "path":
                    if (
                        trail != ("dns", "servers")
                        or obj.get("type") != "https"
                        or not isinstance(value, str)
                        or not value.startswith("/")
                        or ".." in value
                    ):
                        fail()
                if key == "detour":
                    fail()
                walk(value, (*trail, key))
        elif isinstance(obj, list):
            for value in obj:
                walk(value, trail)

    walk(config)
    inbounds, outbounds, route = (
        config.get("inbounds", []),
        config.get("outbounds", []),
        config.get("route", {}),
    )
    if len(inbounds) != 1 or len(outbounds) != 1:
        fail()
    inbound, outbound = inbounds[0], outbounds[0]
    if name in {"ingress.json", "exit.json"}:
        if inbound.get("type") != "trojan" or not inbound.get("tls", {}).get("enabled"):
            fail()
    elif inbound.get("type") != "socks":
        fail()
    if name == "ingress.json":
        if outbound.get("type") != "socks" or (
            outbound.get("server"),
            outbound.get("server_port"),
        ) != ("10.89.0.2", 1081):
            fail()
        if route.get("final") != outbound.get("tag") or any(
            r not in ({"ip_version": 6, "action": "reject"}, {"network": "udp", "action": "reject"})
            for r in route.get("rules", [])
        ):
            fail()
    elif name == "bridge.json":
        if (inbound.get("listen"), inbound.get("listen_port")) != ("10.89.0.2", 1081):
            fail()
        if outbound.get("type") != "direct" or outbound.get("bind_interface") != "tun0":
            fail()
        if route != {
            "rules": [{"ip_version": 6, "action": "reject"}],
            "final": outbound.get("tag"),
        }:
            fail()
        if any(
            server.get("bind_interface") != "tun0"
            for server in config.get("dns", {}).get("servers", [])
        ):
            fail()
    elif name in {"carrier.json", "exit.json"}:
        rules = route.get("rules", [])
        if len(rules) != 2 or rules[1] != {"action": "reject"}:
            fail()
        required = {
            "ip_cidr": ["127.0.0.1/32"],
            "port": 1194,
            "network": "tcp",
            "action": "route",
            "outbound": outbound.get("tag"),
        }
        if name == "exit.json":
            required |= {"override_address": "127.0.0.1", "override_port": 1194}
            if outbound.get("type") != "direct":
                fail()
        else:
            if (inbound.get("listen"), inbound.get("listen_port")) != ("10.89.0.1", 1080):
                fail()
            if (
                outbound.get("type") != "trojan"
                or not outbound.get("tls", {}).get("enabled")
                or outbound["tls"].get("insecure")
            ):
                fail()
            if outbound.get("network") != "tcp":
                fail()
        if rules[0] != required or "final" in route:
            fail()


def validate_plan_topology(plan):
    from wayvia.agent.planner import build_plan

    artifacts = {a.resource: a.content for a in plan.actions if a.kind == "install_config"}
    expected_files = (
        {"ingress.json", "bridge.json", "carrier.json", "vpn-client.conf", "manifest.json"}
        if plan.role == "ingress"
        else {"exit.json", "vpn-server.conf", "manifest.json"}
    )
    if set(artifacts) != expected_files:
        fail()
    interface = next((a.interface for a in plan.actions if a.kind == "exit_nat"), None)
    expected = build_plan(
        plan.role,
        plan.revision,
        artifacts,
        plan.baseline,
        probes=plan.probes,
        exit_interface=interface,
    )
    if plan.actions != expected.actions:
        fail()
    manifest = json.loads(artifacts["manifest.json"])
    if manifest.get("role") != plan.role or manifest.get("vpn_interface") != "tun0":
        fail()
    for field in ("entry_server_name", "carrier_server_name", "vpn_server_name"):
        if not isinstance(manifest.get(field), str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", manifest[field]
        ):
            fail()
    if plan.role == "ingress":
        options = [
            shlex.split(line, comments=True) for line in artifacts["vpn-client.conf"].splitlines()
        ]
        if ["verify-x509-name", manifest["vpn_server_name"], "name"] not in options:
            fail()
        carrier = json.loads(artifacts["carrier.json"])
        if carrier["outbounds"][0]["tls"]["server_name"] != manifest["carrier_server_name"]:
            fail()

    subnet = ipaddress.ip_network(manifest["vpn_subnet"])
    required = set()
    for name, text in artifacts.items():
        if name.endswith(".conf"):
            for line in text.splitlines():
                words = shlex.split(line, comments=True)
                if words and words[0] in {"ca", "cert", "key", "tls-crypt", "tls-auth"}:
                    required.add(Path(words[1]).name)
            if name == "vpn-server.conf":
                server = next(
                    shlex.split(line)[1:]
                    for line in text.splitlines()
                    if line.startswith("server ")
                )
                if ipaddress.ip_network("/".join(server)) != subnet:
                    fail()
        elif name in {"ingress.json", "exit.json"}:
            for inbound in json.loads(text)["inbounds"]:
                tls = inbound["tls"]
                required |= {Path(tls["certificate_path"]).name, Path(tls["key_path"]).name}
    if set(manifest.get("required_secret_files", [])) != required:
        raise AdapterError(
            "SECRET_MANIFEST_INVALID",
            "Secret manifest must match every referenced certificate and key",
        )
    if plan.role == "exit" and next(a.subnet for a in plan.actions if a.kind == "exit_nat") != str(
        subnet
    ):
        fail()
    validate_probes(plan)


def validate_probes(plan):
    from wayvia.agent.models import LAYERS

    if len(plan.probes) != len(LAYERS) or {p.layer for p in plan.probes} != LAYERS:
        raise AdapterError(
            "PROBE_INPUT_REQUIRED", "Every distinct path layer needs an actual probe"
        )
    kinds = {
        "access": "trojan_http" if plan.role == "ingress" else "trojan_vpn",
        "carrier": "vpn_handshake",
        "vpn": "vpn_session",
        "egress": "http",
        "dns": "dns",
        "ipv6": "ipv6_guard",
        "return": "route",
    }
    artifacts = {a.resource: a.content for a in plan.actions if a.kind == "install_config"}
    artifact = json.loads(artifacts["ingress.json" if plan.role == "ingress" else "exit.json"])
    inbound = next(i for i in artifact["inbounds"] if i["type"] == "trojan")
    listener = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(inbound["listen"], inbound["listen"])
    for probe in plan.probes:
        if probe.kind != kinds[probe.layer]:
            raise AdapterError(
                "PROBE_INPUT_REQUIRED", "Path layer uses an inappropriate probe kind"
            )
        if probe.layer in {"vpn", "egress", "dns", "ipv6"}:
            namespace = "wayvia-egress" if plan.role == "ingress" else None
            if probe.namespace != namespace:
                fail()
        if probe.layer == "vpn" and probe.expected != "tun0":
            fail()
        if probe.layer == "return" and (
            probe.namespace or not probe.expected or probe.expected == "tun0"
        ):
            fail()
        if probe.layer == "access":
            if probe.namespace or (probe.target, probe.port) != (listener, inbound["listen_port"]):
                fail()
            if plan.role == "ingress":
                egress = next(p for p in plan.probes if p.layer == "egress")
                if (probe.url, probe.expected, probe.response_format, probe.fallback_urls) != (
                    egress.target,
                    egress.expected,
                    egress.response_format,
                    egress.fallback_urls,
                ):
                    fail()
        if probe.layer == "carrier":
            expected = (
                ("10.89.0.1", 1080, "wayvia-egress")
                if plan.role == "ingress"
                else ("127.0.0.1", 1194, None)
            )
            if (probe.target, probe.port, probe.namespace) != expected:
                fail()
