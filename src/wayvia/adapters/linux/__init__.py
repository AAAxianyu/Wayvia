"""Owned Linux resources. Commands never modify the physical default route."""

import base64
import hashlib
import ipaddress
import json
import os
import platform
import re
import secrets
import tempfile
from pathlib import Path

from wayvia.adapters.base import AdapterError, CommandRunner
from wayvia.adapters.openvpn import check_safe_config
from wayvia.adapters.singbox import SingBoxAdapter


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise AdapterError("UNSAFE_PATH", "Symlink resource is forbidden")
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".wayvia-")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content if isinstance(content, bytes) else content.encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def guard_rules(namespace=False):
    if namespace:
        return """table inet wayvia_guard {
 chain output { type filter hook output priority -10; policy drop;
  oifname "lo" accept
  ct state established,related accept
  oifname "tun0" meta nfproto ipv4 accept
  ip daddr 10.89.0.1 tcp dport 1080 accept
  counter drop
 }
 chain input { type filter hook input priority -10; policy drop;
  iifname "lo" accept
  ct state established,related accept
  ip saddr 10.89.0.1 tcp dport 1081 accept
  iifname "wayvia-ns" ip saddr 10.89.0.1 udp dport 1024-65535 accept
 }
}"""
    # Never place a global DROP policy on the host: restrict only owned ports.
    # v2rayA portSharing binds all interfaces, so explicitly admit namespace only.
    return """table inet wayvia_guard {
 chain input { type filter hook input priority -10; policy accept;
  iifname "wayvia-host" ip saddr 10.89.0.2 tcp dport 1080 accept
  tcp dport 1080 counter drop
 }
}"""


def unit_content(unit, config_root=Path("/etc/wayvia")):
    name = unit.removesuffix(".service").removeprefix("wayvia-")
    commands = {
        "ingress": f"/usr/local/bin/sing-box run -c {config_root}/ingress.json",
        "bridge": f"/usr/local/bin/sing-box run -c {config_root}/bridge.json",
        "exit": f"/usr/local/bin/sing-box run -c {config_root}/exit.json",
        "vpn-client": (
            f"/usr/sbin/openvpn --config {config_root}/vpn-client.conf --script-security 1"
            " --management /run/wayvia/vpn-client.sock unix"
        ),
        "vpn-server": (
            f"/usr/sbin/openvpn --config {config_root}/vpn-server.conf --script-security 1"
            " --management /run/wayvia/vpn-server.sock unix"
        ),
        "carrier": (
            "/usr/local/bin/v2raya --address 127.0.0.1:2017 "
            "--config /var/lib/wayvia/v2raya --v2ray-bin /usr/local/bin/v2raya_core "
            "--v2ray-assetsdir /usr/local/share/v2raya --lite"
        ),
    }
    namespace = (
        "NetworkNamespacePath=/run/netns/wayvia-egress\n"
        if name in {"bridge", "vpn-client"}
        else ""
    )
    capabilities = (
        "CAP_NET_ADMIN CAP_NET_BIND_SERVICE" if name.startswith("vpn-") else "CAP_NET_BIND_SERVICE"
    )
    return (
        "[Unit]\nDescription=Wayvia " + name + "\nAfter=network-online.target\n"
        "[Service]\nType=simple\n"
        + namespace
        + "ExecStart="
        + commands[name]
        + "\nCapabilityBoundingSet="
        + capabilities
        + "\nRestart=on-failure\nRestartSec=3\n"
        "UMask=0077\nNoNewPrivileges=yes\nPrivateTmp=yes\n"
        "ProtectHome=yes\nProtectSystem=strict\nReadWritePaths=/var/lib/wayvia /run/wayvia\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )


class LinuxExecutor:
    simulated = False

    def __init__(
        self,
        *,
        runner=None,
        config_dir="/etc/wayvia",
        state_dir="/var/lib/wayvia/agent",
        unit_dir="/etc/systemd/system",
        enforce_environment=True,
    ):
        self.runner = runner or CommandRunner()
        self.config_dir, self.state_dir, self.unit_dir = map(
            Path, (config_dir, state_dir, unit_dir)
        )
        self.enforce_environment = enforce_environment
        self.ownership_path = self.state_dir / "ownership.json"

    def require_environment(self):
        if self.enforce_environment and (platform.system() != "Linux" or os.geteuid() != 0):
            raise AdapterError(
                "ENVIRONMENT_UNSUPPORTED",
                "Network execution requires an authorized Linux root environment",
            )

    def _cmd(self, argv, namespace=None, **kwargs):
        if namespace:
            argv = ["ip", "netns", "exec", namespace] + argv
        return self.runner.run(argv, **kwargs)

    def _owners(self):
        return json.loads(self.ownership_path.read_text()) if self.ownership_path.exists() else {}

    def _key(self, action):
        return f"{action.kind}:{action.namespace or 'host'}:{action.resource}"

    def _claim(self, action, owned=True):
        owners = self._owners()
        if owned:
            owners[self._key(action)] = True
        else:
            owners.pop(self._key(action), None)
        atomic_write(self.ownership_path, json.dumps(owners))

    def inspect_action(self, action):
        if action.kind == "install_config":
            path = self.config_dir / action.resource
            if path.is_symlink():
                raise AdapterError("UNSAFE_PATH", "Configuration symlink is forbidden")
            return {
                "exists": path.exists(),
                "content": base64.b64encode(path.read_bytes()).decode() if path.exists() else None,
                "mode": path.stat().st_mode & 0o777 if path.exists() else 0o600,
            }
        if action.kind == "namespace":
            output = self._cmd(["ip", "netns", "list"]).stdout
            return {"exists": action.resource in {line.split()[0] for line in output.splitlines()}}
        if action.kind == "veth":
            result = self._cmd(["ip", "-j", "link", "show", "dev", action.resource], check=False)
            return {"exists": result.returncode == 0, "state": result.stdout.strip()}
        if action.kind in {"guard", "exit_nat"}:
            result = self._cmd(
                ["nft", "list", "table", "inet", action.resource],
                namespace=action.namespace,
                check=False,
            )
            return {
                "exists": result.returncode == 0,
                "rules": result.stdout if result.returncode == 0 else None,
            }
        if action.kind == "service":
            result = self._cmd(["systemctl", "is-active", action.resource], check=False)
            unit_path = self.unit_dir / action.resource
            if unit_path.is_symlink():
                raise AdapterError("UNSAFE_PATH", "Unit symlink is forbidden")
            return {
                "exists": unit_path.exists(),
                "active": result.stdout.strip() == "active",
                "unit": base64.b64encode(unit_path.read_bytes()).decode()
                if unit_path.exists()
                else None,
            }
        raise AdapterError("ACTION_UNSUPPORTED", "Action implementation is unavailable")

    @staticmethod
    def _normalize_nft_baseline(value, *, counter=False, nft_object=False):
        if isinstance(value, dict):
            normalized = {}
            for key, item in value.items():
                if nft_object and key == "handle":
                    # nftables assigns handles to identify kernel objects; they do
                    # not change the packet-matching behavior of a rule.
                    continue
                if counter and key in {"packets", "bytes"}:
                    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                        raise AdapterError(
                            "BASELINE_INVALID",
                            "nftables returned invalid counter statistics",
                        )
                    continue
                normalized[key] = LinuxExecutor._normalize_nft_baseline(
                    item,
                    counter=(key == "counter"),
                    nft_object=(key in {"table", "chain", "rule"}),
                )
            if counter and (("packets" in value) != ("bytes" in value)):
                raise AdapterError(
                    "BASELINE_INVALID",
                    "nftables returned incomplete counter statistics",
                )
            return normalized
        if isinstance(value, list):
            return [LinuxExecutor._normalize_nft_baseline(item) for item in value]
        return value

    def _inspect_nft_baseline(self, action):
        result = self._cmd(
            ["nft", "-j", "list", "tables", "inet"], namespace=action.namespace, check=False
        )
        if result.returncode:
            raise AdapterError(
                "BASELINE_READ_FAILED",
                "Unable to read nftables state for deployment baseline",
                details={"resource": action.resource, "returncode": result.returncode},
            )
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AdapterError(
                "BASELINE_INVALID",
                "nftables returned invalid JSON for deployment baseline",
                details={"resource": action.resource},
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("nftables"), list):
            raise AdapterError(
                "BASELINE_INVALID",
                "nftables returned an unexpected JSON structure for deployment baseline",
                details={"resource": action.resource},
            )

        table_seen = False
        for entry in payload["nftables"]:
            if not isinstance(entry, dict) or len(entry) != 1:
                raise AdapterError(
                    "BASELINE_INVALID",
                    "nftables returned an unexpected JSON entry for deployment baseline",
                    details={"resource": action.resource},
                )
            kind, obj = next(iter(entry.items()))
            if not isinstance(obj, dict):
                raise AdapterError(
                    "BASELINE_INVALID",
                    "nftables returned an unexpected JSON object for deployment baseline",
                    details={"resource": action.resource},
                )
            if kind == "metainfo":
                continue
            if kind != "table":
                raise AdapterError(
                    "BASELINE_INVALID",
                    "nftables returned an unexpected object while listing tables",
                    details={"resource": action.resource},
                )
            family, name = obj.get("family"), obj.get("name")
            if not isinstance(family, str) or not isinstance(name, str):
                raise AdapterError(
                    "BASELINE_INVALID",
                    "nftables returned a table without its family and name",
                    details={"resource": action.resource},
                )
            if family == "inet" and name == action.resource:
                if table_seen:
                    raise AdapterError(
                        "BASELINE_INVALID",
                        "nftables returned a duplicate Wayvia table",
                        details={"resource": action.resource},
                    )
                table_seen = True

        if not table_seen:
            return {"exists": False, "rules": None}

        result = self._cmd(
            ["nft", "-j", "list", "table", "inet", action.resource],
            namespace=action.namespace,
            check=False,
        )
        if result.returncode:
            raise AdapterError(
                "BASELINE_READ_FAILED",
                "Unable to read nftables state for deployment baseline",
                details={"resource": action.resource, "returncode": result.returncode},
            )
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AdapterError(
                "BASELINE_INVALID",
                "nftables returned invalid JSON for deployment baseline",
                details={"resource": action.resource},
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("nftables"), list):
            raise AdapterError(
                "BASELINE_INVALID",
                "nftables returned an unexpected JSON structure for deployment baseline",
                details={"resource": action.resource},
            )

        table_contents = []
        table_record_seen = False
        for entry in payload["nftables"]:
            if not isinstance(entry, dict) or len(entry) != 1:
                raise AdapterError(
                    "BASELINE_INVALID",
                    "nftables returned an unexpected JSON entry for deployment baseline",
                    details={"resource": action.resource},
                )
            kind, obj = next(iter(entry.items()))
            if not isinstance(obj, dict):
                raise AdapterError(
                    "BASELINE_INVALID",
                    "nftables returned an unexpected JSON object for deployment baseline",
                    details={"resource": action.resource},
                )
            if kind == "metainfo":
                continue
            if kind == "table":
                if obj.get("family") != "inet" or obj.get("name") != action.resource:
                    raise AdapterError(
                        "BASELINE_INVALID",
                        "nftables returned an unexpected table for deployment baseline",
                        details={"resource": action.resource},
                    )
                if table_record_seen:
                    raise AdapterError(
                        "BASELINE_INVALID",
                        "nftables returned a duplicate Wayvia table",
                        details={"resource": action.resource},
                    )
                table_record_seen = True
                table_contents.append(self._normalize_nft_baseline({kind: obj}))
                continue

            if kind not in {"chain", "rule"}:
                raise AdapterError(
                    "BASELINE_INVALID",
                    "nftables returned an unsupported object type for deployment baseline",
                    details={"resource": action.resource, "object_type": kind},
                )

            family, table = obj.get("family"), obj.get("table")
            if family != "inet" or table != action.resource or not table_record_seen:
                raise AdapterError(
                    "BASELINE_INVALID",
                    "nftables returned an unexpected object for deployment baseline",
                    details={"resource": action.resource},
                )
            table_contents.append(self._normalize_nft_baseline({kind: obj}))

        if not table_record_seen:
            raise AdapterError(
                "BASELINE_INVALID",
                "nftables table disappeared while reading deployment baseline",
                details={"resource": action.resource},
            )

        return {
            "exists": True,
            "rules": table_contents,
        }

    def baseline(self, actions):
        if platform.system() != "Linux" and self.enforce_environment:
            return "unsupported:Linux-required"
        state = []
        for action in actions:
            # Namespace table is absent when namespace is absent; do not execute in it.
            if (
                action.namespace
                and not (Path("/run/netns") / action.namespace).exists()
                and self.enforce_environment
            ):
                snapshot = {"exists": False}
            elif action.kind in {"guard", "exit_nat"}:
                snapshot = self._inspect_nft_baseline(action)
            else:
                snapshot = self.inspect_action(action)
            state.append({"key": self._key(action), "snapshot": snapshot})
        host = self._cmd(["ip", "-j", "route", "show", "table", "main"]).stdout
        return hashlib.sha256(
            json.dumps({"resources": state, "routes": host}, sort_keys=True).encode()
        ).hexdigest()

    def preflight(self, plan):
        self.require_environment()
        from wayvia.agent.validation import validate_plan_topology

        validate_plan_topology(plan)
        self.validate_certificates(plan)
        self.validate_host_conflicts(plan)
        for action in plan.actions:
            if (
                action.namespace
                and not (Path("/run/netns") / action.namespace).exists()
                and self.enforce_environment
            ):
                continue
            before = self.inspect_action(action)
            if before["exists"] and self._key(action) not in self._owners():
                raise AdapterError(
                    "RESOURCE_CONFLICT",
                    "An unowned resource occupies a Wayvia name",
                    details={"resource": action.resource},
                )
            if action.kind == "install_config":
                self.validate_content(action.resource, action.content)
        if {p.layer for p in plan.probes} != {
            "access",
            "carrier",
            "vpn",
            "egress",
            "dns",
            "ipv6",
            "return",
        } or any(p.kind == "unsupported" for p in plan.probes):
            raise AdapterError(
                "PROBE_INPUT_REQUIRED", "Provide actual probes for every path layer before apply"
            )

    def validate_certificates(self, plan):
        from wayvia.agent.validation import secret_file

        manifest = next(a.content for a in plan.actions if a.resource == "manifest.json")
        files = json.loads(manifest).get("required_secret_files", [])
        if not isinstance(files, list) or not files:
            raise AdapterError(
                "SECRET_MANIFEST_INVALID", "Required certificate manifest is missing"
            )
        for name in files:
            path = "/etc/wayvia/secrets/" + name
            secret_file(path, must_exist=self.enforce_environment)
            if name.endswith(".crt"):
                try:
                    self._cmd(["openssl", "x509", "-checkend", "0", "-noout", "-in", path])
                except AdapterError as exc:
                    raise AdapterError(
                        "CERTIFICATE_INVALID",
                        "Certificate is invalid or expired",
                        details={"secret_ref": name},
                    ) from exc
                partner = name[:-4] + ".key"
                if partner in files:
                    cert_public = self._cmd(
                        ["openssl", "x509", "-pubkey", "-noout", "-in", path]
                    ).stdout.strip()
                    key_public = self._cmd(
                        ["openssl", "pkey", "-pubout", "-in", "/etc/wayvia/secrets/" + partner]
                    ).stdout.strip()
                    if not cert_public or cert_public != key_public:
                        raise AdapterError(
                            "CERTIFICATE_KEY_MISMATCH",
                            "Certificate does not match its private key",
                            details={"secret_ref": name},
                        )

    def validate_host_conflicts(self, plan):
        interfaces = json.loads(self._cmd(["ip", "-j", "address", "show"]).stdout)
        planned = [ipaddress.ip_network("10.89.0.0/30")] if plan.role == "ingress" else []
        nat = next((a for a in plan.actions if a.kind == "exit_nat"), None)
        if nat:
            planned.append(ipaddress.ip_network(nat.subnet))
            if nat.interface not in {row["ifname"] for row in interfaces}:
                raise AdapterError(
                    "INTERFACE_MISSING", "Explicit physical egress interface is absent"
                )
        owners = self._owners()
        owned_interfaces = set()
        if plan.role == "ingress" and owners.get("veth:host:wayvia-host"):
            owned_interfaces.add("wayvia-host")
        if plan.role == "exit" and any(row["ifname"] == "tun0" for row in interfaces):
            if not owners.get("service:host:wayvia-vpn-server.service"):
                raise AdapterError("TUN_CONFLICT", "Host tun0 is not owned by Wayvia")
            owned_interfaces.add("tun0")
        owned_networks = {}
        for row in interfaces:
            for address in row.get("addr_info", []):
                if address.get("family") != "inet":
                    continue
                network = ipaddress.ip_network(
                    f"{address['local']}/{address['prefixlen']}", strict=False
                )
                if row["ifname"] in owned_interfaces:
                    owned_networks.setdefault(row["ifname"], []).append(network)
                    continue
                if any(network.overlaps(candidate) for candidate in planned):
                    raise AdapterError(
                        "SUBNET_CONFLICT", "Wayvia subnet overlaps an existing host interface"
                    )
        routes = json.loads(self._cmd(["ip", "-j", "route", "show", "table", "all"]).stdout)
        for route in routes:
            destination = route.get("dst", "default")
            if destination == "default":
                continue
            try:
                network = ipaddress.ip_network(destination, strict=False)
            except ValueError:
                continue
            if network.version != 4 or network.prefixlen == 0:
                continue
            # Exempt only kernel-created connected/local routes for the interfaces
            # this role owns. A static/policy route using the same device is not
            # evidence that Wayvia owns the administrator's routing decision.
            if route.get("protocol") in {"kernel", 2} and any(
                network.subnet_of(owned) for owned in owned_networks.get(route.get("dev"), [])
            ):
                continue
            if any(network.overlaps(n) for n in planned):
                raise AdapterError("ROUTE_CONFLICT", "Wayvia subnet overlaps an existing route")
        sockets = self._cmd(["ss", "-H", "-ltnp"]).stdout.splitlines()
        ports = (
            {1080: "wayvia-carrier.service"}
            if plan.role == "ingress"
            else {1194: "wayvia-vpn-server.service"}
        )
        config_name = "ingress.json" if plan.role == "ingress" else "exit.json"
        data = json.loads(next(a.content for a in plan.actions if a.resource == config_name))
        ports[data["inbounds"][0]["listen_port"]] = (
            "wayvia-ingress.service" if plan.role == "ingress" else "wayvia-exit.service"
        )
        for line in sockets:
            fields = line.split()
            if len(fields) < 4:
                continue
            for port, unit in ports.items():
                if not fields[3].endswith(":" + str(port)):
                    continue
                pids = re.findall(r"pid=(\d+)", line)
                owned = bool(pids)
                for pid in pids:
                    path = Path("/proc") / pid / "cgroup"
                    if not path.exists() or unit not in path.read_text():
                        owned = False
                if not owned:
                    raise AdapterError(
                        "PORT_CONFLICT",
                        "A required listen port belongs to another service",
                        details={"port": port},
                    )

    def validate_content(self, name, text):
        from wayvia.agent.validation import validate_server_artifact

        validate_server_artifact(name, text, must_exist=self.enforce_environment)
        if name.endswith(".conf"):
            check_safe_config(text)
        elif name.endswith(".json"):
            try:
                value = json.loads(text)
            except ValueError as exc:
                raise AdapterError("CONFIG_INVALID", "Configuration JSON is invalid") from exc

            def walk(obj):
                if isinstance(obj, dict):
                    for key, val in obj.items():
                        if key in {"certificate_path", "key_path"}:
                            if (
                                not isinstance(val, str)
                                or not val.startswith("/etc/wayvia/secrets/")
                                or ".." in val
                            ):
                                raise AdapterError(
                                    "SECRET_PATH_FORBIDDEN",
                                    "TLS paths must reference Wayvia secrets",
                                )
                            path = Path(val)
                            if path.exists() and (path.is_symlink() or path.stat().st_mode & 0o077):
                                raise AdapterError(
                                    "SECRET_PERMISSIONS", "Secret material must be owner-only"
                                )
                        if key in {"output", "cache_file"} and val:
                            raise AdapterError(
                                "CONFIG_PATH_FORBIDDEN",
                                "Arbitrary component file paths are forbidden",
                            )
                        walk(val)
                elif isinstance(obj, list):
                    for val in obj:
                        walk(val)

            walk(value)

    def capture(self, action):
        before = self.inspect_action(action)
        before["owned"] = self._key(action) in self._owners()
        return before

    def apply(self, action):
        self._claim(action)  # Durable before a command may create only part of a resource.
        if action.kind == "install_config":
            self.validate_content(action.resource, action.content)
            atomic_write(self.config_dir / action.resource, action.content)
            if action.resource.endswith(".json") and action.resource != "manifest.json":
                SingBoxAdapter(self.runner).validate(self.config_dir / action.resource)
        elif action.kind == "namespace":
            if not self.inspect_action(action)["exists"]:
                self._cmd(["ip", "netns", "add", action.resource])
                self._cmd(["ip", "-n", action.resource, "link", "set", "lo", "up"])
        elif action.kind == "veth":
            if not self.inspect_action(action)["exists"]:
                self._cmd(
                    [
                        "ip",
                        "link",
                        "add",
                        "wayvia-host",
                        "type",
                        "veth",
                        "peer",
                        "name",
                        "wayvia-ns",
                    ]
                )
                self._cmd(["ip", "link", "set", "wayvia-ns", "netns", "wayvia-egress"])
                self._cmd(["ip", "addr", "add", "10.89.0.1/30", "dev", "wayvia-host"])
                self._cmd(["ip", "link", "set", "wayvia-host", "up"])
                self._cmd(
                    ["ip", "-n", "wayvia-egress", "addr", "add", "10.89.0.2/30", "dev", "wayvia-ns"]
                )
                self._cmd(["ip", "-n", "wayvia-egress", "link", "set", "wayvia-ns", "up"])
                self._cmd(
                    ["ip", "-n", "wayvia-egress", "route", "add", "default", "via", "10.89.0.1"]
                )
        elif action.kind in {"guard", "exit_nat"}:
            if action.kind == "guard":
                rules = guard_rules(bool(action.namespace))
            else:
                # Namespace-side forwarding must be enabled separately during install,
                # never silently alter a preexisting global sysctl in a transaction.
                value = self._cmd(["sysctl", "-n", "net.ipv4.ip_forward"]).stdout.strip()
                if value != "1":
                    raise AdapterError(
                        "FORWARDING_DISABLED",
                        "Enable forwarding explicitly on the authorized Japanese host "
                        "before planning",
                    )
                rules = f'''table inet wayvia_exit {{
 chain postrouting {{ type nat hook postrouting priority srcnat; policy accept;
 ip saddr {action.subnet} oifname "{action.interface}" counter masquerade
 }}
}}'''
            existing = self.inspect_action(action)["exists"]
            batch = (f"delete table inet {action.resource}\n" if existing else "") + rules
            self._cmd(["nft", "--check", "-f", "-"], namespace=action.namespace, input_text=batch)
            self._cmd(["nft", "-f", "-"], namespace=action.namespace, input_text=batch)
        elif action.kind == "service":
            atomic_write(
                self.unit_dir / action.resource,
                unit_content(action.resource, self.config_dir),
                mode=0o644,
            )
            self._cmd(["systemctl", "daemon-reload"])
            operation = "stop" if action.operation == "stop" else "restart"
            self._cmd(["systemctl", operation, action.resource])
            if operation != "stop":
                self._cmd(["systemctl", "is-active", "--quiet", action.resource])
            if action.resource == "wayvia-carrier.service" and operation != "stop":
                self.configure_carrier()

    def configure_carrier(self):
        from wayvia.adapters.v2raya import V2rayAAdapter, new_management_password

        auth_path = self.config_dir / "secrets" / "v2raya-management.json"
        if not auth_path.exists():
            atomic_write(
                auth_path, json.dumps({"username": "wayvia", "password": new_management_password()})
            )
        auth = json.loads(auth_path.read_text())
        adapter = V2rayAAdapter(runner=self.runner)
        adapter.wait_ready()
        create = not adapter._request("GET", "account")["hasAnyAccounts"]
        adapter.configure_carrier(
            json.loads((self.config_dir / "carrier.json").read_text()),
            auth["username"],
            auth["password"],
            create=create,
        )

    def rollback(self, action, before):
        if action.kind == "install_config":
            path = self.config_dir / action.resource
            if before["exists"]:
                atomic_write(path, base64.b64decode(before["content"]), before["mode"])
            else:
                path.unlink(missing_ok=True)
        elif action.kind == "service":
            self._cmd(["systemctl", "stop", action.resource])
            path = self.unit_dir / action.resource
            if before["exists"]:
                atomic_write(path, base64.b64decode(before["unit"]), mode=0o644)
            else:
                path.unlink(missing_ok=True)
            self._cmd(["systemctl", "daemon-reload"])
            # Restarts happen after all previous files/guards are restored.
        elif action.kind == "veth" and not before["exists"]:
            if self.inspect_action(action)["exists"]:
                self._cmd(["ip", "link", "delete", "wayvia-host"])
        elif action.kind == "namespace" and not before["exists"]:
            if self.inspect_action(action)["exists"]:
                self._cmd(["ip", "netns", "delete", "wayvia-egress"])
        elif action.kind in {"guard", "exit_nat"}:
            present = self.inspect_action(action)["exists"]
            batch = (f"delete table inet {action.resource}\n" if present else "") + (
                before.get("rules") or ""
            )
            if batch:
                self._cmd(["nft", "-f", "-"], namespace=action.namespace, input_text=batch)
        self._claim(action, before.get("owned", False))

    def restore_services(self, steps):
        for step in steps:
            action, before = step["action"], step["before"]
            if action.kind == "service" and before.get("active"):
                self._cmd(["systemctl", "restart", action.resource])
                if action.resource == "wayvia-carrier.service":
                    self.configure_carrier()

    def protect(self, plan):
        # Stop accepted ingress first, retaining the namespace guard and the carrier
        # path so the data plane cannot acquire a physical fallback while reconnecting.
        unit = "wayvia-ingress.service" if plan.role == "ingress" else "wayvia-exit.service"
        self._cmd(["systemctl", "stop", unit])

    def prepare_ingress(self, action):
        if action.kind != "install_config" or action.resource != "ingress.json":
            return action
        from wayvia.agent.probe_runtime import PROBE_USER

        path = self.state_dir / "probe-password"
        if not path.exists():
            atomic_write(path, secrets.token_urlsafe(32))
        config = json.loads(action.content)
        inbound = next(i for i in config["inbounds"] if i["type"] == "trojan")
        inbound["users"] = [u for u in inbound["users"] if u["name"] != PROBE_USER]
        inbound["users"].append({"name": PROBE_USER, "password": path.read_text()})
        return action.model_copy(update={"content": json.dumps(config)})

    def sync_devices(self, devices, revision):
        path = self.config_dir / "ingress.json"
        config = json.loads(path.read_text())
        inbounds = [item for item in config.get("inbounds", []) if item.get("type") == "trojan"]
        if len(inbounds) != 1:
            raise AdapterError("INGRESS_INVALID", "Exactly one owned Trojan ingress is required")
        self.validate_devices(devices)
        inbounds[0]["users"] = [{"name": d["id"], "password": d["password"]} for d in devices]
        try:
            from wayvia.agent.models import Action

            effective = self.prepare_ingress(
                Action(kind="install_config", resource="ingress.json", content=json.dumps(config))
            )
            atomic_write(path, effective.content)
            SingBoxAdapter(self.runner).validate(path)
            self._cmd(["systemctl", "restart", "wayvia-ingress.service"])
            self._cmd(["systemctl", "is-active", "--quiet", "wayvia-ingress.service"])
        except Exception:
            self._cmd(["systemctl", "stop", "wayvia-ingress.service"])
            raise
        return {
            "status": "applied",
            "revision": revision,
            "evidence": {
                "validated": True,
                "old_sessions": "closed_by_process_restart",
                "device_count": len(devices),
            },
        }

    def validate_devices(self, devices):
        if (
            not isinstance(devices, list)
            or len(devices) > 4096
            or any(
                set(d) != {"id", "password"}
                or not isinstance(d["id"], str)
                or d["id"] == "wayvia-internal-probe"
                or not isinstance(d["password"], str)
                or len(d["password"]) < 24
                or len(d["password"]) > 256
                for d in devices
            )
        ):
            raise AdapterError("DEVICE_INPUT_INVALID", "Invalid bounded device credential list")
        if len({d["id"] for d in devices}) != len(devices) or len(
            {d["password"] for d in devices}
        ) != len(devices):
            raise AdapterError(
                "DEVICE_INPUT_INVALID", "Device identifiers and credentials must be independent"
            )
