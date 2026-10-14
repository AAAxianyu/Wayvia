"""Run the production Linux executor against owned disposable kernel resources."""

import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wayvia.adapters.base import AdapterError
from wayvia.adapters.linux import LinuxExecutor
from wayvia.agent.models import Action, Plan


def verify_nft_baseline(executor, action):
    baseline_before = executor.baseline([action])
    raw_before = executor.inspect_action(action)["rules"]
    counters_before = [
        tuple(map(int, values))
        for values in re.findall(r"counter packets (\d+) bytes (\d+)", raw_before)
    ]
    assert counters_before, "expected a live nftables counter in the guard table"

    packet = (
        "import socket; s=socket.socket(); s.settimeout(2); "
        "s.connect_ex(('10.89.0.1', 65534)); s.close()"
    )
    subprocess.run(
        ["ip", "netns", "exec", action.namespace, sys.executable, "-c", packet],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    raw_after = executor.inspect_action(action)["rules"]
    counters_after = [
        tuple(map(int, values))
        for values in re.findall(r"counter packets (\d+) bytes (\d+)", raw_after)
    ]
    assert len(counters_after) == len(counters_before)
    assert any(
        after[0] > before[0] and after[1] > before[1]
        for before, after in zip(counters_before, counters_after, strict=True)
    ), "the denied connection did not increment the live nftables counter"
    baseline_after_counter = executor.baseline([action])
    assert baseline_after_counter == baseline_before, "counter-only traffic changed the baseline"

    now = datetime.now(UTC)
    plan = Plan(
        role="ingress",
        revision="nft-baseline-regression",
        baseline=baseline_before,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        actions=(action,),
        probes=(),
    ).seal()
    subprocess.run(
        [
            "ip",
            "netns",
            "exec",
            action.namespace,
            "nft",
            "add",
            "rule",
            "inet",
            action.resource,
            "input",
            "tcp",
            "dport",
            "65534",
            "accept",
            "comment",
            "wayvia-baseline-regression",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    baseline_after_rule_change = executor.baseline([action])
    assert baseline_after_rule_change != baseline_before, "a real rule change was not detected"
    try:
        plan.check(baseline_after_rule_change, now=now)
    except AdapterError as error:
        assert error.code == "BASELINE_CHANGED"
    else:
        raise AssertionError("the stale plan was not rejected after a real rule change")

    return {
        "status": "passed",
        "counter_before": counters_before,
        "counter_after": counters_after,
        "counter_only_baseline_unchanged": True,
        "structural_rule_change_detected": True,
        "stale_plan_rejected": True,
    }


def run():
    executor = LinuxExecutor(
        config_dir="/tmp/wayvia-cfg", state_dir="/tmp/wayvia-state", unit_dir="/tmp/wayvia-units"
    )
    actions = [
        Action(kind="namespace", resource="wayvia-egress"),
        Action(kind="guard", resource="wayvia_guard", namespace="wayvia-egress"),
        Action(kind="veth", resource="wayvia-host"),
        Action(kind="guard", resource="wayvia_guard"),
    ]
    default_before = subprocess.check_output(["ip", "-j", "route", "show", "default"], text=True)
    steps = []
    try:
        for action in actions:
            before = executor.capture(action)
            steps.append((action, before))
            executor.apply(action)
        observed = {
            "namespace": subprocess.check_output(["ip", "netns", "list"], text=True),
            "veth": json.loads(
                subprocess.check_output(["ip", "-j", "address", "show", "wayvia-host"], text=True)
            ),
            "guard": subprocess.check_output(
                [
                    "ip",
                    "netns",
                    "exec",
                    "wayvia-egress",
                    "nft",
                    "list",
                    "table",
                    "inet",
                    "wayvia_guard",
                ],
                text=True,
            ),
            "host_guard": subprocess.check_output(
                ["nft", "list", "table", "inet", "wayvia_guard"], text=True
            ),
        }
        assert "policy drop" in observed["guard"]
        assert any(a.get("local") == "10.89.0.1" for a in observed["veth"][0]["addr_info"])
        baseline_regression = verify_nft_baseline(executor, actions[1])
    finally:
        for action, before in reversed(steps):
            executor.rollback(action, before)
    default_after = subprocess.check_output(["ip", "-j", "route", "show", "default"], text=True)
    assert default_after == default_before
    assert not subprocess.check_output(["ip", "netns", "list"], text=True).strip()
    assert (
        subprocess.run(["ip", "link", "show", "wayvia-host"], capture_output=True).returncode != 0
    )
    assert (
        subprocess.run(
            ["nft", "list", "table", "inet", "wayvia_guard"], capture_output=True
        ).returncode
        != 0
    )
    return {
        "status": "passed",
        "executor": "production LinuxExecutor with real CommandRunner",
        "actions": [a.model_dump() for a in actions],
        "observed": observed,
        "baseline_regression": baseline_regression,
        "rollback": {
            "namespace_removed": True,
            "veth_removed": True,
            "owned_guard_removed": True,
            "host_default_route_unchanged": True,
        },
    }


if __name__ == "__main__":
    try:
        result = run()
    except Exception as error:
        result = {"status": "failed", "error": str(error)}
    Path("/evidence/executor-result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    sys.exit(0 if result["status"] == "passed" else 1)
