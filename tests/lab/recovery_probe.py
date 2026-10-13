"""Real independent systemd timer and new-process recovery in a disposable container.

Never run against a workstation or existing gateway. The subprocess deliberately
exits with an APPLYING journal; no health probes or ACTIVE state are fabricated.
"""

import argparse
import json
import os
import platform
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wayvia.adapters.linux import LinuxExecutor
from wayvia.agent.models import Action, Plan, State
from wayvia.agent.transactions import SystemdTimer, TransactionManager

BASE = Path("/tmp/wayvia-recovery-lab")
PYTHON = "/opt/wayvia-lab-python/bin/python"
EVIDENCE = Path("/evidence/recovery-result.json")


def require_isolation():
    if (
        platform.system() != "Linux"
        or os.geteuid() != 0
        or not Path("/.dockerenv").exists()
        or os.environ.get("WAYVIA_RECOVERY_LAB") != "1"
        or Path("/proc/1/comm").read_text().strip() != "systemd"
    ):
        raise RuntimeError("Requires explicitly authorized disposable Docker systemd environment")


def command(argv, *, check=True, timeout=20):
    return subprocess.run(argv, capture_output=True, text=True, check=check, timeout=timeout)


def resources_absent():
    namespaces = command(["ip", "netns", "list"]).stdout
    return (
        "wayvia-egress" not in namespaces
        and command(["ip", "link", "show", "wayvia-host"], check=False).returncode != 0
        and command(["nft", "list", "table", "inet", "wayvia_guard"], check=False).returncode != 0
    )


def manager(directory):
    config = directory / "config"
    state = directory / "state"
    units = directory / "units"
    executor = LinuxExecutor(config_dir=config, state_dir=state, unit_dir=units)
    timer = SystemdTimer(executor.runner, python=PYTHON, config_dir=config, unit_dir=units)
    return TransactionManager(executor, state, timer)


def controller(mode):
    require_isolation()
    directory = BASE / mode
    m = manager(directory)
    actions = (
        Action(kind="namespace", resource="wayvia-egress"),
        Action(kind="guard", resource="wayvia_guard", namespace="wayvia-egress"),
        Action(kind="veth", resource="wayvia-host"),
        Action(kind="guard", resource="wayvia_guard"),
    )
    now = datetime.now(UTC)
    plan = Plan(
        role="ingress",
        revision="recovery-lab",
        baseline=m.executor.baseline(actions),
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        confirm_seconds=10,
        actions=actions,
        probes=(),
    ).seal()
    tx = {
        "id": uuid.uuid4().hex,
        "role": "ingress",
        "revision": "recovery-lab",
        "state": State.APPLYING,
        "plan": plan.model_dump(mode="json"),
        "steps": [],
        "deadline": (now + timedelta(seconds=10)).isoformat(),
    }
    m._save(tx)
    if mode == "timer":
        m.timer.arm(tx["id"], 10, m.root)
    for action in actions:
        before = m.executor.capture(action)
        tx["steps"].append(
            {"action": action.model_dump(mode="json"), "before": before, "restored": False}
        )
        m._save(tx)
        m.executor.apply(action)
    # Parent verifies kernel state and the durable pending journal after exit.
    print(
        json.dumps({"id": tx["id"], "state": tx["state"], "journal_dir": str(m.root)}), flush=True
    )
    return 0


def run():
    require_isolation()
    if BASE.exists() or not resources_absent():
        raise RuntimeError("Recovery lab requires a fresh container with no owned-name conflicts")
    default_before = command(["ip", "-j", "route", "show", "default"]).stdout
    observations = []
    for mode in ("timer", "restart"):
        child = command([PYTHON, str(Path(__file__).resolve()), "--controller", mode], timeout=30)
        info = json.loads(child.stdout.strip())
        journal = Path(info["journal_dir"]) / (info["id"] + ".json")
        initial = json.loads(journal.read_text())
        assert initial["state"] == "APPLYING"
        assert not resources_absent()
        units_before = command(["systemctl", "list-timers", "--all", "--no-pager"]).stdout
        if mode == "timer":
            assert "wayvia-rollback-" + info["id"] in units_before
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                if json.loads(journal.read_text())["state"] == "ROLLED_BACK":
                    break
                time.sleep(0.25)
            final = json.loads(journal.read_text())
            assert final["state"] == "ROLLED_BACK", (
                "Independent systemd timer did not restore pending resources"
            )
            service = command(
                [
                    "systemctl",
                    "show",
                    "wayvia-rollback-" + info["id"] + ".service",
                    "--property=Result",
                    "--property=ExecMainStatus",
                ],
                check=False,
            )
            service_result = service.stdout
            if (
                service.returncode == 0
                and service_result.strip()
                and "Result=success" not in service_result
            ):
                raise RuntimeError("Independent recovery service did not finish successfully")
            evidence = {
                "systemd_timer_listed": True,
                "recovery_service": service_result.strip()
                or "transient unit collected after completion",
                "controller_exited_before_rollback": True,
            }
        else:
            independent = command(
                [
                    PYTHON,
                    "-m",
                    "wayvia.agent.recovery",
                    "--journal-dir",
                    info["journal_dir"],
                    "--config-dir",
                    str(BASE / mode / "config"),
                    "--unit-dir",
                    str(BASE / mode / "units"),
                ],
                timeout=30,
            )
            final = json.loads(journal.read_text())
            assert final["state"] == "ROLLED_BACK"
            evidence = {
                "independent_new_process_exit": independent.returncode,
                "recovery_stdout": json.loads(independent.stdout),
                "controller_exited_before_recovery": True,
            }
        assert resources_absent()
        assert command(["ip", "-j", "route", "show", "default"]).stdout == default_before
        assert all(step["restored"] for step in final["steps"])
        observations.append(
            {
                "case": mode,
                "state_before": "APPLYING",
                "state_after": final["state"],
                "actions_recovered": len(final["steps"]),
                "kernel_resources_removed": True,
                "host_default_route_unchanged": True,
                **evidence,
            }
        )
    return {
        "status": "passed",
        "scope": "disposable Linux systemd container",
        "timer": "production SystemdTimer and systemd-run",
        "recovery": "independent production wayvia.agent.recovery processes",
        "cases": observations,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--controller", choices=("timer", "restart"))
    args = parser.parse_args()
    if args.controller:
        return controller(args.controller)
    try:
        result = run()
    except Exception as error:
        result = {"status": "failed", "error_type": type(error).__name__, "error": str(error)}
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
