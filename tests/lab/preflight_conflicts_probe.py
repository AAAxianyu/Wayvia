"""Real Linux preflight conflict checks in an explicitly disposable container.

No runner is replaced. Kernel interfaces/routes and the production ownership
file are fixtures; this experiment does not start OpenVPN or certify its health.
"""

import json
import os
import platform
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from wayvia.adapters.base import AdapterError
from wayvia.adapters.linux import LinuxExecutor
from wayvia.agent.models import Action, Probe
from wayvia.agent.planner import build_plan
from wayvia.agent.validation import validate_plan_topology, validate_server_artifact
from wayvia.config import WayviaConfig, example_config
from wayvia.render import ServerRenderer


def command(argv, *, check=True):
    return subprocess.run(
        argv, check=check, capture_output=True, text=True, timeout=10, shell=False
    )


def snapshot():
    return {
        "addresses": json.loads(command(["ip", "-j", "-4", "address", "show"]).stdout),
        "routes": json.loads(command(["ip", "-j", "-4", "route", "show", "table", "all"]).stdout),
        "rules": json.loads(command(["ip", "-j", "rule", "show"]).stdout),
        "unrelated_nft": json.loads(
            command(["nft", "-j", "list", "table", "inet", "unrelated_preflight"]).stdout
        ),
    }


def plan(role):
    config = WayviaConfig.model_validate(example_config())
    artifacts = ServerRenderer(config).render(
        role, [], {"carrier-credential": secrets.token_urlsafe(32)}
    )
    ns = "wayvia-egress" if role == "ingress" else None
    url, expected = "http://192.0.2.2:8080", "192.0.2.1"
    probes = (
        Probe(
            layer="access",
            kind="trojan_http" if ns else "trojan_vpn",
            target="127.0.0.1",
            port=443,
            **({"url": url, "expected": expected} if ns else {}),
        ),
        Probe(
            layer="carrier",
            kind="vpn_handshake",
            target="10.89.0.1" if ns else "127.0.0.1",
            port=1080 if ns else 1194,
            namespace=ns,
        ),
        Probe(layer="vpn", kind="vpn_session", target="10.77.0.1", expected="tun0", namespace=ns),
        Probe(layer="egress", kind="http", target=url, expected=expected, namespace=ns),
        Probe(
            layer="dns", kind="dns", target="192.0.2.2", port=53, expected="test.lab", namespace=ns
        ),
        Probe(layer="ipv6", kind="ipv6_guard", namespace=ns),
        Probe(layer="return", kind="route", target="192.0.2.2", expected="wv-egress"),
    )
    prepared = build_plan(
        role,
        config.revision,
        artifacts,
        "isolated-preflight-observation",
        probes=probes,
        exit_interface="wv-egress",
    )
    validate_plan_topology(prepared)
    for name, content in artifacts.items():
        validate_server_artifact(name, content, must_exist=False)
    return prepared


def run(output):
    if (
        platform.system() != "Linux"
        or os.geteuid() != 0
        or not Path("/.dockerenv").exists()
        or os.environ.get("WAYVIA_LAB_DISPOSABLE") != "1"
    ):
        raise RuntimeError("Explicitly disposable Linux root container required")
    for name in ("tun0", "wv-egress", "wayvia-host"):
        if command(["ip", "link", "show", "dev", name], check=False).returncode == 0:
            raise RuntimeError("Fresh container required; reserved interface already exists")
    if command(["ip", "netns", "list"]).stdout.strip():
        raise RuntimeError("Fresh container without network namespaces required")
    if (
        command(["nft", "list", "table", "inet", "unrelated_preflight"], check=False).returncode
        == 0
    ):
        raise RuntimeError("Fresh container without the fixture nft table required")
    default_before = command(["ip", "-j", "route", "show", "default"]).stdout
    rules_before = command(["ip", "-j", "rule", "show"]).stdout
    command(["ip", "link", "add", "wv-egress", "type", "dummy"])
    command(["ip", "address", "add", "192.0.2.1/24", "dev", "wv-egress"])
    command(["ip", "link", "set", "wv-egress", "up"])
    command(["nft", "add", "table", "inet", "unrelated_preflight"])
    output["versions"] = {
        "kernel": platform.release(),
        "iproute2": command(["ip", "-Version"]).stdout.strip(),
        "nftables": command(["nft", "--version"]).stdout.strip(),
    }
    with tempfile.TemporaryDirectory(prefix="wayvia-preflight-") as temporary:
        root = Path(temporary)
        executor = LinuxExecutor(
            config_dir=root / "config", state_dir=root / "state", unit_dir=root / "units"
        )
        executor.require_environment()
        plans = {role: plan(role) for role in ("ingress", "exit")}
        steps = []

        def check(name, role, expected=None):
            before = snapshot()
            ownership = executor._owners()
            error = None
            try:
                executor.validate_host_conflicts(plans[role])
            except AdapterError as exc:
                error = exc.code
            assert error == expected, (name, error, expected)
            assert snapshot() == before, (name, "preflight modified kernel resources")
            assert executor._owners() == ownership, (name, "preflight changed ownership")
            output["cases"].append(
                {
                    "name": name,
                    "role": role,
                    "status": "passed",
                    "result": error or "accepted",
                    "kernel_snapshot_unchanged": True,
                    "ownership_unchanged": True,
                }
            )

        try:
            command(["ip", "tuntap", "add", "dev", "tun0", "mode", "tun"])
            check("exit_rejects_unowned_unaddressed_tun", "exit", "TUN_CONFLICT")
            command(["ip", "address", "add", "10.42.0.1/24", "dev", "tun0"])
            command(["ip", "link", "set", "tun0", "up"])
            check("exit_rejects_unowned_unrelated_tun", "exit", "TUN_CONFLICT")
            command(["ip", "address", "flush", "dev", "tun0"])
            command(["ip", "address", "add", "10.77.0.1/24", "dev", "tun0"])
            check("exit_rejects_unowned_overlapping_tun", "exit", "TUN_CONFLICT")
            owner = Action(kind="service", resource="wayvia-vpn-server.service")
            executor._claim(owner)
            check("owned_exit_tun_kernel_routes_accepted", "exit")
            command(["ip", "rule", "add", "priority", "1101", "lookup", "101"])
            command(
                [
                    "ip",
                    "route",
                    "add",
                    "10.77.0.128/25",
                    "dev",
                    "tun0",
                    "proto",
                    "static",
                    "table",
                    "101",
                ]
            )
            check("owned_tun_does_not_exempt_static_policy_route", "exit", "ROUTE_CONFLICT")
            command(["ip", "route", "flush", "table", "101"])
            command(["ip", "rule", "delete", "priority", "1101"])
            executor._claim(owner, False)
            command(["ip", "link", "delete", "tun0"])
            for name, table, destination in (
                ("static_main_route", "main", "10.77.0.0/24"),
                ("narrower_policy_route", "101", "10.77.0.64/26"),
                ("broader_policy_route", "101", "10.0.0.0/8"),
            ):
                if table != "main":
                    command(["ip", "rule", "add", "priority", "1101", "lookup", table])
                command(
                    [
                        "ip",
                        "route",
                        "add",
                        destination,
                        "dev",
                        "wv-egress",
                        "proto",
                        "static",
                        "table",
                        table,
                    ]
                )
                check(name + "_rejected", "exit", "ROUTE_CONFLICT")
                command(["ip", "route", "delete", destination, "table", table])
                if table != "main":
                    command(["ip", "rule", "delete", "priority", "1101"])
            command(["ip", "route", "add", "blackhole", "10.77.0.0/24", "table", "103"])
            check("blackhole_policy_table_rejected", "exit", "ROUTE_CONFLICT")
            command(["ip", "route", "flush", "table", "103"])
            check("clean_exit_accepted", "exit")
            command(["ip", "tuntap", "add", "dev", "tun0", "mode", "tun"])
            command(["ip", "address", "add", "10.42.0.1/24", "dev", "tun0"])
            command(["ip", "link", "set", "tun0", "up"])
            for action in (
                Action(kind="namespace", resource="wayvia-egress"),
                Action(kind="veth", resource="wayvia-host"),
            ):
                steps.append((action, executor.capture(action)))
                executor.apply(action)
            check("owned_ingress_veth_with_unrelated_host_vpn_accepted", "ingress")
            veth = Action(kind="veth", resource="wayvia-host")
            executor._claim(veth, False)
            check("unowned_reserved_veth_rejected", "ingress", "SUBNET_CONFLICT")
            executor._claim(veth)
        finally:
            for action, before in reversed(steps):
                executor.rollback(action, before)
            command(["ip", "rule", "delete", "priority", "1101"], check=False)
            command(["ip", "route", "flush", "table", "101"], check=False)
            command(["ip", "route", "flush", "table", "103"], check=False)
            command(["ip", "link", "delete", "tun0"], check=False)
            command(["ip", "link", "delete", "wv-egress"])
            assert command(["nft", "list", "table", "inet", "unrelated_preflight"]).returncode == 0
            command(["nft", "delete", "table", "inet", "unrelated_preflight"])
    assert command(["ip", "-j", "route", "show", "default"]).stdout == default_before
    assert command(["ip", "-j", "rule", "show"]).stdout == rules_before
    assert not command(["ip", "netns", "list"]).stdout.strip()
    for name in ("tun0", "wv-egress", "wayvia-host"):
        assert command(["ip", "link", "show", "dev", name], check=False).returncode != 0
    output["cleanup"] = {
        "fixture_interfaces_and_namespace_removed": True,
        "fixture_policy_rules_removed": True,
        "original_default_route_unchanged": True,
        "unrelated_nft_preserved_until_fixture_teardown": True,
    }
    output["status"] = "passed"


if __name__ == "__main__":
    result = {
        "status": "running",
        "simulated": False,
        "executor": "LinuxExecutor.validate_host_conflicts with real CommandRunner",
        "scope": "disposable real Linux kernel preflight; no component-health claim",
        "cases": [],
    }
    try:
        run(result)
    except Exception as error:
        result.update(status="failed", error=str(error))
    Path("/evidence/preflight-conflicts-result.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result))
    sys.exit(0 if result["status"] == "passed" else 1)
