"""Run the production Linux executor against owned disposable kernel resources."""

import json
import subprocess
import sys
from pathlib import Path

from wayvia.adapters.linux import LinuxExecutor
from wayvia.agent.models import Action


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
