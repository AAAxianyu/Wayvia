import json

import pytest

from wayvia.adapters.base import AdapterError
from wayvia.agent.models import Action, Probe
from wayvia.agent.planner import build_plan
from wayvia.agent.validation import (
    validate_plan_topology,
    validate_probes,
    validate_server_artifact,
)
from wayvia.config import WayviaConfig, example_config
from wayvia.render import ServerRenderer


def probes():
    return (
        Probe(layer="access", kind="tcp", target="127.0.0.1", port=443),
        Probe(
            layer="carrier", kind="tcp", target="10.89.0.1", port=1080, namespace="wayvia-egress"
        ),
        Probe(
            layer="vpn",
            kind="route",
            target="203.0.113.2",
            expected="tun0",
            namespace="wayvia-egress",
        ),
        Probe(
            layer="egress",
            kind="http",
            target="https://example.test/exit",
            expected="203.0.113.2",
            namespace="wayvia-egress",
        ),
        Probe(
            layer="dns",
            kind="dns",
            target="1.1.1.1",
            port=53,
            expected="example.test",
            namespace="wayvia-egress",
        ),
        Probe(layer="ipv6", kind="ipv6_guard", namespace="wayvia-egress"),
        Probe(layer="return", kind="route", target="198.51.100.2", expected="eth0"),
    )


def artifacts(role="ingress", **changes):
    data = example_config()
    data["vpn"].update(changes)
    config = WayviaConfig.model_validate(data)
    return ServerRenderer(config).render(
        role, [{"id": "one", "password": "p" * 32}], {"carrier-credential": "c" * 32}
    )


def test_all_rendered_artifacts_obey_privileged_schema():
    for role in ("ingress", "exit"):
        for name, content in artifacts(role).items():
            validate_server_artifact(name, content)


def test_complete_plan_guard_order_and_role_is_enforced():
    plan = build_plan("ingress", "1", artifacts(), "baseline", probes=probes())
    validate_plan_topology(plan)
    unsafe = plan.model_copy(
        update={
            "actions": (
                Action(kind="service", resource="wayvia-ingress.service", operation="restart"),
            )
        }
    ).seal()
    with pytest.raises(AdapterError) as err:
        validate_plan_topology(unsafe)
    assert err.value.code == "TOPOLOGY_FORBIDDEN"
    reversed_steps = plan.model_copy(update={"actions": tuple(reversed(plan.actions))}).seal()
    with pytest.raises(AdapterError):
        validate_plan_topology(reversed_steps)


def test_exit_nat_is_derived_from_configured_subnet():
    plan = build_plan(
        "exit", "1", artifacts("exit", subnet="10.78.0.0/24"), "baseline", exit_interface="ens3"
    )
    nat = next(action for action in plan.actions if action.kind == "exit_nat")
    assert nat.subnet == "10.78.0.0/24"
    assert nat.interface == "ens3"


def test_layers_cannot_be_empty_or_mislabel_service_health():
    plan = build_plan("ingress", "1", artifacts(), "baseline", probes=probes())
    for candidate in (
        plan.model_copy(update={"probes": ()}),
        plan.model_copy(
            update={
                "probes": tuple(
                    Probe(layer=p.layer, kind="service", target="wayvia-ingress.service")
                    for p in plan.probes
                )
            }
        ),
        plan.model_copy(
            update={
                "probes": tuple(
                    p.model_copy(update={"namespace": None}) if p.layer == "egress" else p
                    for p in plan.probes
                )
            }
        ),
    ):
        with pytest.raises(AdapterError):
            validate_probes(candidate)


def test_server_config_cannot_bypass_bridge_or_exit_allowlist():
    entry = json.loads(artifacts()["ingress.json"])
    entry["outbounds"][0] = {"type": "direct", "tag": "vpn-business"}
    with pytest.raises(AdapterError):
        validate_server_artifact("ingress.json", json.dumps(entry))
    exit_config = json.loads(artifacts("exit")["exit.json"])
    exit_config["route"]["rules"] = [{"action": "route", "outbound": "vpn-only"}]
    with pytest.raises(AdapterError):
        validate_server_artifact("exit.json", json.dumps(exit_config))


def test_https_dns_url_path_is_allowed_but_component_file_path_is_not():
    config = json.loads(artifacts()["carrier.json"])
    config["dns"]["servers"][0].update(type="https", path="/dns-query")
    validate_server_artifact("carrier.json", json.dumps(config))
    config["outbounds"][0]["path"] = "/etc/passwd"
    # A path with a core transport role cannot be admitted even though DNS HTTPS
    # uses the same key name; the real parser subsequently validates DNS fields.
    with pytest.raises(AdapterError):
        validate_server_artifact("carrier.json", json.dumps(config))
