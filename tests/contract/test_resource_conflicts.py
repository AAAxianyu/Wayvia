"""Preflight ownership and overlap checks using read-only Linux observations."""

import json

import pytest

from wayvia.adapters.base import AdapterError, CommandResult
from wayvia.adapters.linux import LinuxExecutor
from wayvia.agent.planner import build_plan
from wayvia.config import WayviaConfig, example_config
from wayvia.render import ServerRenderer


def interface(name, address=None, prefix=24):
    return {
        "ifname": name,
        "addr_info": [{"family": "inet", "local": address, "prefixlen": prefix}] if address else [],
    }


class Observations:
    def __init__(self, interfaces=(), routes=()):
        self.interfaces = [interface("lo", "127.0.0.1", 8), interface("eth0", "192.0.2.4")]
        self.interfaces.extend(interfaces)
        self.routes = [
            {"dst": "default", "dev": "eth0", "gateway": "192.0.2.1"},
            {"dst": "192.0.2.0/24", "dev": "eth0", "protocol": "kernel"},
            *routes,
        ]
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        if argv == ["ip", "-j", "address", "show"]:
            return CommandResult(0, json.dumps(self.interfaces))
        if argv == ["ip", "-j", "route", "show", "table", "all"]:
            return CommandResult(0, json.dumps(self.routes))
        if argv == ["ss", "-H", "-ltnp"]:
            return CommandResult(0, "")
        raise AssertionError(f"Unexpected command: {argv}")


def check(tmp_path, role="exit", *, interfaces=(), routes=(), owners=None):
    config = WayviaConfig.model_validate(example_config())
    artifacts = ServerRenderer(config).render(role, [], {"carrier-credential": "c" * 32})
    plan = build_plan(role, config.revision, artifacts, "baseline", exit_interface="eth0")
    observations = Observations(interfaces, routes)
    executor = LinuxExecutor(runner=observations, state_dir=tmp_path, enforce_environment=False)
    executor._owners = lambda: owners or {}
    executor.validate_host_conflicts(plan)
    return observations.calls


@pytest.mark.parametrize("address", [None, "10.77.0.1", "10.42.0.1"])
def test_exit_rejects_every_unowned_host_tun0(tmp_path, address):
    with pytest.raises(AdapterError) as error:
        check(tmp_path, interfaces=[interface("tun0", address)])
    assert error.value.code == "TUN_CONFLICT"


def test_client_unit_ownership_does_not_claim_exit_host_tun(tmp_path):
    with pytest.raises(AdapterError) as error:
        check(
            tmp_path,
            interfaces=[interface("tun0", "10.77.0.1")],
            owners={"service:host:wayvia-vpn-client.service": True},
        )
    assert error.value.code == "TUN_CONFLICT"


def test_owned_exit_tun_and_its_kernel_routes_are_valid_on_reapply(tmp_path):
    calls = check(
        tmp_path,
        interfaces=[interface("tun0", "10.77.0.1")],
        owners={"service:host:wayvia-vpn-server.service": True},
        routes=[
            {"dst": "10.77.0.0/24", "dev": "tun0", "protocol": "kernel"},
            {
                "dst": "10.77.0.1",
                "dev": "tun0",
                "protocol": "kernel",
                "type": "local",
                "table": "local",
            },
            {
                "dst": "10.77.0.255",
                "dev": "tun0",
                "protocol": "kernel",
                "type": "broadcast",
                "table": "local",
            },
        ],
    )
    assert ["ip", "-j", "route", "show", "table", "all"] in calls
    assert len(calls) == 3


def test_ingress_preserves_unrelated_host_vpn_and_owned_veth(tmp_path):
    check(
        tmp_path,
        "ingress",
        interfaces=[interface("tun0", "10.42.0.1"), interface("wayvia-host", "10.89.0.1", 30)],
        owners={"veth:host:wayvia-host": True},
        routes=[
            {"dst": "10.42.0.0/24", "dev": "tun0", "protocol": "kernel"},
            {"dst": "10.89.0.0/30", "dev": "wayvia-host", "protocol": "kernel"},
            {
                "dst": "10.89.0.1",
                "dev": "wayvia-host",
                "protocol": "kernel",
                "type": "local",
                "table": "local",
            },
        ],
    )


@pytest.mark.parametrize("device", ["tun0", "wayvia-host", "lo"])
def test_ingress_rejects_overlapping_unowned_interface_even_with_reserved_name(tmp_path, device):
    with pytest.raises(AdapterError) as error:
        check(tmp_path, "ingress", interfaces=[interface(device, "10.89.0.1", 30)])
    assert error.value.code == "SUBNET_CONFLICT"


@pytest.mark.parametrize("table", ["main", 101, 103])
@pytest.mark.parametrize("destination", ["10.77.0.0/24", "10.77.0.64/26", "10.0.0.0/8"])
def test_exit_checks_static_and_policy_tables_for_subnet_overlap(tmp_path, table, destination):
    with pytest.raises(AdapterError) as error:
        check(
            tmp_path,
            routes=[{"dst": destination, "dev": "eth0", "protocol": "static", "table": table}],
        )
    assert error.value.code == "ROUTE_CONFLICT"


def test_blackhole_route_without_device_is_still_a_conflict(tmp_path):
    with pytest.raises(AdapterError) as error:
        check(tmp_path, routes=[{"dst": "10.77.0.0/24", "type": "blackhole", "table": 105}])
    assert error.value.code == "ROUTE_CONFLICT"


@pytest.mark.parametrize("device", ["lo", "tun0"])
def test_interface_name_does_not_exempt_administrator_policy_routes(tmp_path, device):
    with pytest.raises(AdapterError) as error:
        check(
            tmp_path,
            interfaces=[interface("tun0", "10.77.0.1")],
            owners={"service:host:wayvia-vpn-server.service": True},
            routes=[{"dst": "10.77.0.128/25", "dev": device, "protocol": "static", "table": 101}],
        )
    assert error.value.code == "ROUTE_CONFLICT"


def test_owned_interface_exemption_is_scoped_to_the_role(tmp_path):
    with pytest.raises(AdapterError) as error:
        check(
            tmp_path,
            interfaces=[interface("wayvia-host", "10.77.0.1")],
            owners={"veth:host:wayvia-host": True},
        )
    assert error.value.code == "SUBNET_CONFLICT"


def test_default_and_unrelated_policy_routes_remain_untouched(tmp_path):
    calls = check(
        tmp_path,
        routes=[
            {"dst": "0.0.0.0/0", "dev": "eth0", "table": 101},
            {"dst": "10.78.0.0/24", "dev": "eth0", "table": 102},
        ],
    )
    assert calls == [
        ["ip", "-j", "address", "show"],
        ["ip", "-j", "route", "show", "table", "all"],
        ["ss", "-H", "-ltnp"],
    ]
