"""v2rayA 2.5.10 public HTTP contract, verified against the locked source tag.

No database writes or transparent proxy activation. Use a dedicated Wayvia
instance with the identically versioned v2raya_core, never an existing instance.
"""

import ipaddress
import re
import secrets
from urllib.parse import quote, urlencode, urlsplit

import httpx

from wayvia.adapters.base import AdapterError, CommandRunner


class V2rayAAdapter:
    simulated = False
    version = "2.5.10"

    def __init__(self, endpoint="http://127.0.0.1:2017", *, client=None, token=None, runner=None):
        url = urlsplit(endpoint)
        if url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise AdapterError("MANAGEMENT_ENDPOINT_FORBIDDEN", "v2rayA management must be local")
        self.client = client or httpx.Client(base_url=endpoint, timeout=10)
        self.token = token
        self.runner = runner or CommandRunner()

    def _request(self, method, path, body=None):
        try:
            response = self.client.request(
                method,
                "/api/" + path,
                json=body,
                headers={"Authorization": "Bearer " + self.token} if self.token else {},
            )
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AdapterError(
                "V2RAYA_UNAVAILABLE", "v2rayA returned no valid bounded response"
            ) from exc
        if not isinstance(result, dict):
            raise AdapterError(
                "V2RAYA_INVALID_RESPONSE", "v2rayA response must be a structured object"
            )
        if response.status_code != 200 or result.get("code") != "SUCCESS":
            raise AdapterError(
                "V2RAYA_REQUEST_FAILED",
                "v2rayA rejected the requested operation",
                details={
                    "operation": path,
                    "status": response.status_code,
                    "errorCode": result.get("errorCode", "UNKNOWN"),
                },
            )
        return result.get("data")

    def inspect(self):
        data = self._request("GET", "version")
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("version"), str)
            or not data["version"]
            or not isinstance(data.get("coreVersionValid"), bool)
        ):
            raise AdapterError(
                "V2RAYA_INVALID_RESPONSE", "v2rayA version response is incomplete or invalid"
            )
        return {
            k: data.get(k)
            for k in ("version", "coreVersion", "coreVersionValid", "serviceValid", "variant")
        }

    def validate(self):
        info = self.inspect()
        core_version = info.get("coreVersion")
        if (
            info["version"].removeprefix("v") != self.version
            or info["coreVersionValid"] is not True
            or not isinstance(core_version, str)
            or core_version.removeprefix("v") != self.version
        ):
            raise AdapterError(
                "VERSION_MISMATCH", "v2rayA requires the locked matching v2raya_core", details=info
            )
        return info

    def login(self, username, password, *, create=False):
        if create and self._request("GET", "account")["hasAnyAccounts"]:
            raise AdapterError("RESOURCE_CONFLICT", "Dedicated v2rayA account already exists")
        self.token = self._request(
            "POST", "account" if create else "login", {"username": username, "password": password}
        )["token"]

    def configure_carrier(self, config, username, password, *, create=False):
        self.validate()
        self.login(username, password, create=create)
        outbounds = [o for o in config.get("outbounds", []) if o.get("type") == "trojan"]
        if len(outbounds) != 1:
            raise AdapterError(
                "CARRIER_CONFIG_INVALID", "Carrier must have exactly one Trojan outbound"
            )
        node = outbounds[0]
        tls = node.get("tls", {})
        if not tls.get("enabled") or tls.get("insecure"):
            raise AdapterError("TLS_REQUIRED", "Carrier server verification must remain enabled")
        address = self.resolve_carrier(node, config)
        if ":" in address:
            address = "[" + address + "]"
        uri = (
            "trojan://"
            + quote(node["password"], safe="")
            + "@"
            + address
            + ":"
            + str(node["server_port"])
            + "?"
            + urlencode({"sni": tls.get("server_name", node["server"]), "allowInsecure": "0"})
            + "#Wayvia-carrier"
        )
        previous = {
            "ports": self._request("GET", "ports"),
            "setting": self._request("GET", "setting")["setting"],
            "touch": self._request("GET", "touch"),
        }
        self._request(
            "PUT", "setting", {"transparent": "close", "portSharing": True, "dnsMode": "off"}
        )
        self._request(
            "PUT",
            "ports",
            {
                "socks5": 1080,
                "http": 0,
                "socks5WithPac": 0,
                "httpWithPac": 0,
                "vmess": 0,
                "api": {"port": 0, "services": []},
            },
        )
        touch = self._request("GET", "touch")["touch"]
        owned = [s for s in touch["servers"] if s["name"] == "Wayvia-carrier"]
        which = (
            {"_type": "server", "id": owned[0]["id"], "sub": 0, "outbound": "proxy"}
            if owned
            else None
        )
        body = {"url": uri, "kind": "server"}
        if which:
            body["which"] = which
        imported = self._request("POST", "import", body)["touch"]["servers"]
        selected = next(s for s in imported if s["name"] == "Wayvia-carrier")
        self._request(
            "PUT",
            "outboundConnections",
            {
                "outbound": "proxy",
                "touches": [
                    {"_type": "server", "id": selected["id"], "sub": 0, "outbound": "proxy"}
                ],
            },
        )
        self._request("POST", "v2ray")
        return {"previous": previous, "version": self.version, "configured": True}

    def resolve_carrier(self, node, config):
        try:
            return str(ipaddress.ip_address(node["server"]))
        except ValueError:
            pass
        if not isinstance(node["server"], str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", node["server"]
        ):
            raise AdapterError("CARRIER_ENDPOINT_INVALID", "Carrier hostname is invalid")
        servers = config.get("dns", {}).get("servers", [])
        resolver = next(
            (server for server in servers if server.get("tag") == node.get("domain_resolver")), None
        )
        if not resolver or resolver.get("type") not in {"udp", "tcp"}:
            raise AdapterError(
                "BOOTSTRAP_DNS_UNSUPPORTED",
                "Carrier bootstrap DNS requires explicit UDP or TCP resolver",
            )
        try:
            resolver_ip = str(ipaddress.ip_address(resolver["server"]))
        except (ValueError, KeyError) as exc:
            raise AdapterError(
                "BOOTSTRAP_DNS_UNSUPPORTED", "Bootstrap resolver address must be numeric"
            ) from exc
        argv = [
            "dig",
            "+short",
            "+tries=1",
            "+time=3",
            "@" + resolver_ip,
            "-p",
            str(resolver.get("server_port", 53)),
            node["server"],
            "A",
        ]
        if resolver["type"] == "tcp":
            argv.insert(1, "+tcp")
        output = self.runner.run(argv, timeout=5).stdout
        addresses = []
        for line in output.splitlines():
            try:
                address = ipaddress.ip_address(line.strip())
                if address.version == 4:
                    addresses.append(str(address))
            except ValueError:
                pass
        if not addresses:
            raise AdapterError(
                "BOOTSTRAP_DNS_FAILED", "Explicit bootstrap resolver returned no IPv4 address"
            )
        return sorted(addresses)[0]

    def rollback(self, previous):
        self._request("DELETE", "v2ray")
        self._request("PUT", "ports", previous["ports"])
        self._request("PUT", "setting", previous["setting"])
        touches = previous["touch"]["touch"]["connectedServer"]
        self._request("PUT", "outboundConnections", {"outbound": "proxy", "touches": touches})
        if previous["touch"].get("running"):
            self._request("POST", "v2ray")

    def stop(self):
        self._request("DELETE", "v2ray")


def new_management_password():
    return secrets.token_urlsafe(24)
