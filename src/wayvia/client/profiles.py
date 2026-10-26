"""Bounded effective-profile inspection and private offline staging; no core control."""

from pathlib import Path

import yaml

from wayvia.adapters.linux import atomic_write
from wayvia.client import GROUP, NODE, TEST_URL
from wayvia.config import _UniqueLoader, split_endpoint
from wayvia.errors import WayviaError
from wayvia.observation_run import ObservationError, private

MAX_BYTES = 32 * 1024 * 1024


def read_profile(path):
    path = Path(path).absolute()
    try:
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError()
        private(path)
        if path.stat().st_size > MAX_BYTES:
            raise ValueError()
        raw = path.read_text()
        value = yaml.load(raw, Loader=_UniqueLoader)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (
        OSError,
        UnicodeError,
        yaml.YAMLError,
        ValueError,
        TypeError,
        RecursionError,
        ObservationError,
    ):
        raise WayviaError(
            "CLIENT_PROFILE_INVALID", "Use a valid bounded private Mihomo YAML file"
        ) from None


def inspect(profile, reference=None):
    """Report fixed codes only. Raw config, endpoints, credentials and domains stay private."""
    errors, warnings = [], []
    try:
        if not isinstance(profile, dict):
            raise ValueError()
        proxies, groups, rules = (profile.get(k) for k in ("proxies", "proxy-groups", "rules"))
        if (
            not isinstance(proxies, list)
            or len(proxies) != 1
            or not isinstance(proxies[0], dict)
            or not isinstance(groups, list)
            or len(groups) > 64
            or not all(isinstance(g, dict) for g in groups)
            or not isinstance(rules, list)
            or len(rules) > 200000
            or not all(isinstance(r, str) for r in rules)
        ):
            raise ValueError()
        proxy = proxies[0]
        group = next((g for g in groups if g.get("name") == GROUP), {})
        checks = {
            "CLIENT_UNREVIEWED_EXTENSIONS": not any(
                profile.get(key)
                for key in (
                    "listeners",
                    "tunnels",
                    "proxy-providers",
                    "rule-providers",
                    "sub-rules",
                    "script",
                )
            ),
            "CLIENT_NODE_IDENTITY": proxy.get("name") == NODE and proxy.get("type") == "trojan",
            "CLIENT_TLS_VERIFICATION": proxy.get("skip-cert-verify") is False
            and bool(proxy.get("sni")),
            "CLIENT_PROXY_CREDENTIAL": isinstance(proxy.get("password"), str)
            and bool(proxy["password"]),
            "CLIENT_GROUP_POLICY": group.get("type") == "select"
            and group.get("proxies") == [NODE]
            and group.get("empty-fallback") == "REJECT"
            and not group.get("use")
            and not group.get("include-all")
            and not group.get("include-all-proxies")
            and not group.get("include-all-providers"),
            "CLIENT_PROTECTED_DEFAULT": profile.get("mode") == "rule"
            and rules[-1:] == ["MATCH,Wayvia"],
            "CLIENT_IPV6_GUARD": "IP-CIDR6,::/0,REJECT,no-resolve" in rules
            and profile.get("ipv6") is True,
            "CLIENT_LOCAL_LISTENER": profile.get("allow-lan") is False
            and type(profile.get("mixed-port")) is int
            and 1 <= profile["mixed-port"] <= 65535,
            "CLIENT_SELECTION_CACHE": profile.get("profile", {}).get("store-selected") is True,
            "CLIENT_MANUAL_DELAY": group.get("interval") == 0 and group.get("url") == TEST_URL,
        }
        dns, tun = profile.get("dns", {}), profile.get("tun", {})
        checks["CLIENT_DNS_POLICY"] = (
            dns.get("enable") is True
            and dns.get("respect-rules") is True
            and dns.get("nameserver")
            and all(str(v).endswith("#Wayvia") for v in dns["nameserver"])
        )
        if tun.get("enable") is True:
            checks["CLIENT_TUN_ROUTE_OVERRIDE"] = not any(
                value
                for key, value in tun.items()
                if key.startswith(("route-", "include-", "exclude-"))
            )
            checks["CLIENT_TUN_GUARD"] = (
                tun.get("auto-route") is True
                and tun.get("strict-route") is True
                and tun.get("auto-detect-interface") is True
                and set(tun.get("dns-hijack", [])) == {"any:53", "tcp://any:53"}
            )
        else:
            warnings.append("CLIENT_SYSTEM_PROXY_PARTIAL_COVERAGE")
        controller = profile.get("external-controller")
        if controller:
            host, _ = split_endpoint(controller)
            checks["CLIENT_CONTROLLER_EXPOSURE"] = host in {"127.0.0.1", "::1", "localhost"}
        if reference is not None:
            if not isinstance(reference, dict):
                raise ValueError()
            checks["CLIENT_ROUTE_OVERRIDE"] = rules == reference.get("rules")
            checks["CLIENT_DNS_OVERRIDE"] = dns == reference.get("dns")
            checks["CLIENT_PROXY_OVERRIDE"] = proxies == reference.get("proxies")
            checks["CLIENT_MTU_OVERRIDE"] = tun.get("mtu") == reference.get("tun", {}).get("mtu")
        else:
            warnings.append("CLIENT_REFERENCE_REQUIRED_FOR_ROUTE_AND_DNS_EQUIVALENCE")
        errors.extend(code for code, passed in checks.items() if not passed)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        errors.append("CLIENT_PROFILE_STRUCTURE")
    return {
        "schema_version": 1,
        "status": "rejected" if errors else "compatible",
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "capture": "tun_requested"
        if isinstance(profile, dict)
        and isinstance(profile.get("tun"), dict)
        and profile["tun"].get("enable") is True
        else "application_proxy_only",
        "verification": {
            "scope": "offline_contract_not_runtime_connectivity",
            "reference_compared": reference is not None,
            "os_permissions": "unknown",
            "actual_egress": "unknown",
            "business_impact": "unknown",
        },
        "next_action": (
            "Review overrides against a fresh trusted subscription; "
            "check Verge activation, service/TUN permissions and effective configuration. "
            "Keep the last valid profile."
        ),
    }


def stage(candidate, destination, reference, *, selected=None):
    """Explicit offline install into an owner-private export path; never reloads a client."""
    report = inspect(candidate, reference)
    if report["errors"]:
        raise WayviaError(
            "CLIENT_UPDATE_REJECTED",
            "Candidate rejected; previous file retained",
            details={"codes": report["errors"]},
        )
    destination = Path(destination).absolute()
    try:
        if any(p.is_symlink() for p in (destination, *destination.parents)):
            raise ValueError()
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        private(destination.parent, directory=True)
        if destination.exists():
            private(destination)
        raw = yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True)
        if len(raw.encode()) > MAX_BYTES:
            raise ValueError()
        atomic_write(destination, raw)
    except (ValueError, OSError, ObservationError, yaml.YAMLError):
        raise WayviaError(
            "CLIENT_UPDATE_STORAGE", "Profile persistence is unconfirmed; inspect private storage"
        ) from None
    preserved = selected in {NODE, "Wayvia-entry", None}
    return {
        "status": "staged",
        "selection": NODE,
        "selection_preserved": preserved,
        "legacy_identity_migrated": selected == "Wayvia-entry",
        "client_reloaded": False,
        "unknown_selection": selected is not None and not preserved,
        "verification": report["verification"],
    }


def private_json(value, destination):
    from wayvia.telemetry.report import export_json

    return export_json(value, destination)
