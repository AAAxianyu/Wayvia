import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from wayvia.adapters.base import AdapterError
from wayvia.adapters.linux import LinuxExecutor
from wayvia.adapters.openvpn import check_safe_config
from wayvia.agent.models import Action, Probe, ShapingSpec
from wayvia.agent.planner import build_plan
from wayvia.agent.validation import validate_plan_topology
from wayvia.config import WayviaConfig, example_config
from wayvia.render import ServerRenderer

SHAPING = {"egress_kbit": 1800, "carrier_kbit": 1200, "notsent_lowat": 131072}


def render_exit(**overrides):
    data = example_config()
    data.update(overrides)
    config = WayviaConfig.model_validate(data)
    return config, ServerRenderer(config).render(
        "exit", [], {"carrier-credential": "carrier-password"}
    )


def test_unset_tuning_keeps_existing_revisions():
    config = WayviaConfig.model_validate(example_config())
    value = config.model_dump(mode="json")
    del value["shaping"]
    del value["vpn"]["tcp_queue_limit"], value["vpn"]["socket_buffer"]
    del value["policy"]["proxy_domains"]
    body = json.dumps(value, sort_keys=True, separators=(",", ":"))
    assert config.revision == hashlib.sha256(body.encode()).hexdigest()
    shaped = WayviaConfig.model_validate({**example_config(), "shaping": SHAPING})
    assert shaped.revision != config.revision


@pytest.mark.parametrize(
    "shaping",
    [
        {"egress_kbit": 1800, "carrier_kbit": 1800},
        {"egress_kbit": 100, "carrier_kbit": 50},
        {"egress_kbit": 1800, "carrier_kbit": 1200, "notsent_lowat": 1},
    ],
)
def test_invalid_shaping_is_rejected(shaping):
    with pytest.raises(ValidationError):
        WayviaConfig.model_validate({**example_config(), "shaping": shaping})


def test_exit_manifest_carries_shaping_and_plan_derives_it():
    _, files = render_exit(shaping=SHAPING)
    manifest = json.loads(files["manifest.json"])
    assert manifest["shaping"] == {**SHAPING, "carrier_port": 443}
    probes = [
        Probe.model_validate(item)
        for item in json.loads(Path("examples/exit-probes.json").read_text())
    ]
    plan = build_plan("exit", "1", files, "baseline", probes=probes, exit_interface="ens17")
    action = next(a for a in plan.actions if a.kind == "shaping")
    assert action.interface == "ens17"
    assert action.shaping == ShapingSpec(**SHAPING, carrier_port=443)
    kinds = [a.kind for a in plan.actions if a.kind != "install_config"]
    assert kinds == ["exit_nat", "shaping", "service", "service"]
    validate_plan_topology(plan)


def test_unshaped_exit_still_plans_removal_and_ingress_never_shapes():
    _, files = render_exit()
    assert "shaping" not in json.loads(files["manifest.json"])
    plan = build_plan("exit", "1", files, "baseline", exit_interface="ens17")
    assert next(a for a in plan.actions if a.kind == "shaping").shaping is None
    config = WayviaConfig.model_validate({**example_config(), "shaping": SHAPING})
    ingress = ServerRenderer(config).render(
        "ingress", [{"id": "d", "password": "p" * 32}], {"carrier-credential": "c" * 32}
    )
    assert "shaping" not in json.loads(ingress["manifest.json"])


def test_shaping_actions_are_owned_and_parameters_are_confined():
    with pytest.raises(ValidationError):
        Action(kind="shaping", resource="other", interface="ens17")
    with pytest.raises(ValidationError):
        Action(kind="shaping", resource="wayvia_shaping")
    with pytest.raises(ValidationError):
        Action(
            kind="service",
            resource="wayvia-exit.service",
            shaping=ShapingSpec(**SHAPING, carrier_port=443),
        )


def test_vpn_buffer_limits_render_and_pass_the_safety_contract():
    data = example_config()
    data["vpn"] = {**data["vpn"], "tcp_queue_limit": 32, "socket_buffer": 262144}
    _, files = render_exit(vpn=data["vpn"])
    server = files["vpn-server.conf"]
    assert "tcp-queue-limit 32\nsndbuf 262144\nrcvbuf 262144\n" in server
    check_safe_config(server)
    with pytest.raises(AdapterError):
        check_safe_config("sndbuf 1 2\n")
    with pytest.raises(AdapterError):
        check_safe_config("tcp-queue-limit lots\n")


class Runner:
    def __init__(self, root="fq_codel", lowat="4294967295"):
        self.root, self.lowat, self.calls = root, lowat, []

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        stdout = ""
        if argv[:3] == ["tc", "-j", "qdisc"]:
            stdout = json.dumps([{"kind": self.root, "handle": "1:", "root": True}])
        elif argv[:3] == ["tc", "qdisc", "add"] and "root" in argv:
            self.root = "htb"
        elif argv[:3] == ["tc", "qdisc", "del"]:
            self.root = "fq_codel"
        elif argv[:2] == ["sysctl", "-n"]:
            stdout = self.lowat
        elif argv[:2] == ["sysctl", "-w"]:
            self.lowat = argv[2].split("=")[1]
        return SimpleNamespace(stdout=stdout, returncode=0)


def executor(tmp_path, runner):
    return LinuxExecutor(
        runner=runner,
        config_dir=tmp_path / "etc",
        state_dir=tmp_path / "state",
        unit_dir=tmp_path / "units",
        enforce_environment=False,
    )


def shaping_action(spec=True):
    return Action(
        kind="shaping",
        resource="wayvia_shaping",
        interface="ens17",
        shaping=ShapingSpec(**SHAPING, carrier_port=443) if spec else None,
    )


def test_apply_builds_a_fresh_htb_and_rollback_restores_the_host(tmp_path):
    runner = Runner()
    host = executor(tmp_path, runner)
    action = shaping_action()
    before = host.capture(action)
    assert before["exists"] is False and before["notsent_lowat"] == "4294967295"
    host.apply(action)
    commands = [" ".join(argv) for argv in runner.calls]
    assert "tc qdisc del dev ens17 root" in commands
    assert "tc qdisc add dev ens17 root handle 1: htb default 20" in commands
    assert (
        "tc class add dev ens17 parent 1:1 classid 1:10 htb rate 1200kbit ceil 1800kbit prio 0"
        in commands
    )
    assert (
        "tc class add dev ens17 parent 1:1 classid 1:20 htb rate 600kbit ceil 1800kbit prio 1"
        in commands
    )
    assert any("u32 match ip sport 443 0xffff flowid 1:10" in c for c in commands)
    assert runner.lowat == "131072"
    assert host.inspect_action(action)["applied"]["carrier_kbit"] == 1200
    host.apply(action)  # Re-application rebuilds instead of stacking filters.
    assert sum(c.startswith("tc filter add") for c in commands) == 2
    host.rollback(action, before)
    assert runner.root == "fq_codel"
    assert runner.lowat == "4294967295"
    assert host.inspect_action(action)["applied"] is None


def test_disabling_removes_only_wayvia_shaping(tmp_path):
    runner = Runner()
    host = executor(tmp_path, runner)
    host.apply(shaping_action())
    host.apply(shaping_action(spec=False))
    assert runner.root == "fq_codel" and runner.lowat == "4294967295"
    foreign = Runner(root="cake")
    other = executor(tmp_path / "other", foreign)
    other.apply(shaping_action(spec=False))
    assert foreign.root == "cake"
    assert not any(argv[:2] in (["tc", "qdisc"], ["sysctl", "-w"]) for argv in foreign.calls)


def test_rollback_to_an_earlier_wayvia_shaping_reapplies_it(tmp_path):
    runner = Runner()
    host = executor(tmp_path, runner)
    host.apply(shaping_action())
    action = Action(
        kind="shaping",
        resource="wayvia_shaping",
        interface="ens17",
        shaping=ShapingSpec(egress_kbit=4000, carrier_kbit=3000, carrier_port=443),
    )
    before = host.capture(action)
    host.apply(action)
    host.rollback(action, before)
    state = host.inspect_action(action)
    assert state["applied"]["egress_kbit"] == 1800
    assert state["original_notsent_lowat"] == "4294967295"
    assert runner.lowat == "131072"


def test_foreign_root_discipline_blocks_enabling(tmp_path):
    runner = Runner(root="cake")
    host = executor(tmp_path, runner)
    assert host.inspect_action(shaping_action())["exists"] is True
    assert host.inspect_action(shaping_action())["root"] == "cake"
