#!/usr/bin/env python3
"""Container-side actions for installed, real systemd/agent/API transactions.

Only the transaction lab's explicitly disposable containers may invoke this.
All service paths are the production paths; no executor or probe is simulated.
"""

import argparse
import hashlib
import json
import os
import pwd
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import yaml

CONTROL = Path("/var/lib/wayvia/control")
WAYVIA = "/opt/wayvia/.venv/bin/wayvia"
PYTHON = "/opt/wayvia/.venv/bin/python"
INPUTS = Path("/inputs")


def command(argv, *, accepted=(0,), timeout=180):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode not in accepted:
        # CLI errors are already redacted; avoid including arbitrary command output.
        code = "command_failed"
        public_error = {}
        for line in [result.stdout, result.stderr, *(result.stderr + result.stdout).splitlines()]:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    if "code" in value:
                        code = value["code"]
                    public_error = value
            except ValueError:
                continue
        raise RuntimeError(
            f"{Path(argv[0]).name} failed: exit={result.returncode}, code={code}, "
            + result.stderr[-1600:]
            + json.dumps(public_error)
        )
    return result


def cli(*args, accepted=(0,), timeout=180):
    result = command(
        ["runuser", "-u", "wayvia", "--", WAYVIA, "--system", *args],
        accepted=accepted,
        timeout=timeout,
    )
    return json.loads(result.stdout)


def api(path, *, method="GET", payload=None, timeout=120):
    token = (CONTROL / "secrets/admin-token").read_text().strip()
    request = urllib.request.Request(
        "http://127.0.0.1:8787" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Idempotency-Key": uuid.uuid4().hex,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        result = json.load(error)
        raise RuntimeError(f"API {path.split('/')[3]} failed: {result.get('code')}") from None


def agent_rows(status):
    rows = status.get("agent") if isinstance(status, dict) else None
    if not isinstance(rows, list):
        if isinstance(rows, dict) and rows.get("status") == "unknown":
            raise RuntimeError("AGENT_STATUS_UNKNOWN: " + str(rows.get("code", "unspecified")))
        raise RuntimeError("AGENT_STATUS_INVALID: expected a transaction list")
    if any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("AGENT_STATUS_INVALID: expected transaction objects")
    return rows


def wait_agent_ready(timeout=10):
    """Wait only for the service user's real status request to reach the agent."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("AGENT_READINESS_TIMEOUT: status=unknown code=agent_unavailable")
        status = cli("status", "--json", timeout=min(5, remaining))
        rows = status.get("agent") if isinstance(status, dict) else None
        if rows == {"status": "unknown", "code": "agent_unavailable"}:
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            continue
        agent_rows(status)  # Empty first-install journals are ready; business health is separate.
        return status


def wait_control_ready(timeout=10):
    wait_agent_ready(timeout)
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("API_READINESS_TIMEOUT: authenticated API did not start")
        try:
            api("/api/v1/devices", timeout=min(5, remaining))
            return
        except OSError:
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def owned_write(path, data):
    user = pwd.getpwnam("wayvia")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(path.parent, user.pw_uid, user.pw_gid)
    path.write_text(data)
    path.chmod(0o600)
    os.chown(path, user.pw_uid, user.pw_gid)


def software_install():
    architecture = "arm64" if os.uname().machine == "aarch64" else "amd64"
    wheels = list(Path("/workspace/dist").glob("*.whl"))
    assert len(wheels) == 1
    result = command(
        [
            "python3.12",
            "/workspace/deploy/install.py",
            "--system",
            "--arch",
            architecture,
            "--components",
            "/components",
            "--wheel",
            str(wheels[0]),
            "--uv",
            "/opt/wayvia-lab-python/bin/uv",
        ]
    )
    installed = json.loads(result.stdout)
    assert installed["status"] == "installed_inactive"
    assert installed["network_changed"] is False and installed["services_started"] is False
    return installed


def install(role):
    if not Path("/proc/1/comm").read_text().strip() == "systemd":
        raise RuntimeError("real systemd PID 1 is required")
    software_install()
    owned_write(CONTROL / "config.yaml", (INPUTS / "config.yaml").read_text())
    for source in (INPUTS / "certificates").iterdir():
        if source.suffix not in {".crt", ".key"}:
            continue
        target = Path("/etc/wayvia/secrets") / source.name
        shutil.copyfile(source, target)
        target.chmod(0o600)
    shutil.copyfile(
        INPUTS / "certificates/vpn-ca.crt", "/usr/local/share/ca-certificates/wayvia-lab.crt"
    )
    command(["update-ca-certificates"])
    owned_write(CONTROL / "incoming/carrier-password", (INPUTS / "carrier-password").read_text())
    cli("secret", "import", "carrier-credential", str(CONTROL / "incoming/carrier-password"))
    owned_write(CONTROL / "probes.json", (INPUTS / (role + "-probes.json")).read_text())
    if role == "exit":
        command(["sysctl", "-w", "net.ipv4.ip_forward=1"])
    before = command(["ip", "-j", "route", "show", "default"]).stdout
    Path("/var/lib/wayvia/lab-default-route.json").write_text(before)
    command(["nft", "add", "table", "inet", "unrelated_lab"])
    command(["ip", "netns", "add", "unrelated-lab"])
    command(
        [
            "systemctl",
            "start",
            "wayvia-recover.service",
            "wayvia-agent.service",
            "wayvia-api.service",
        ]
    )
    wait_control_ready()
    return {"status": "installed_and_started", "role": role, "simulated": False}


def device():
    result = api(
        "/api/v1/devices", method="POST", payload={"name": "lab-phone", "platform": "android"}
    )
    owned_write(CONTROL / "lab-device.json", json.dumps(result))
    assert result["status"] == "pending"
    assert cli("device", "list")["devices"][0]["id"] == result["id"]
    return {"id": result["id"], "status": result["status"], "api_cli_same_store": True}


def make_plan(role):
    plan = CONTROL / (role + "-plan.json")
    cli(
        "render",
        str(CONTROL / "config.yaml"),
        "--role",
        role,
        "--output",
        str(CONTROL / "rendered"),
    )
    args = [
        "plan",
        str(CONTROL / "config.yaml"),
        "--role",
        role,
        "--baseline",
        "auto",
        "--probes",
        str(CONTROL / "probes.json"),
        "--output-file",
        str(plan),
    ]
    if role == "exit":
        args += ["--exit-interface", "eth0"]
    cli(*args)
    return plan


def apply(role, *, monitor_previous=None, test_profile=None):
    plan = make_plan(role)
    result = cli("apply", str(plan), accepted=(0, 6))
    assert result["state"] == "AWAITING_CONFIRM", result
    if monitor_previous is not None:
        from wayvia.adapters.linux import LinuxExecutor
        from wayvia.agent.transactions import TransactionManager

        previous = TransactionManager(LinuxExecutor(), "/var/lib/wayvia/agent").monitor(
            monitor_previous
        )
        assert previous["current"] is False
        assert current_state(role)["id"] == result["id"]
        request_profile(test_profile)
    confirmed = cli("confirm", result["id"])
    assert confirmed["state"] == "ACTIVE", confirmed
    return {
        "id": result["id"],
        "state": confirmed["state"],
        "role": role,
        "revision": confirmed["revision"],
    }


def monitor():
    command(["systemctl", "start", "wayvia-monitor.service"], accepted=(0, 1))
    return cli("status", "--json")


def current_state(role):
    rows = agent_rows(cli("status", "--json"))
    current = [
        row
        for row in rows
        if row.get("role") == role
        and row.get("current") is True
        and row.get("state") in {"ACTIVE", "DEGRADED", "AWAITING_CONFIRM"}
    ]
    if len(current) != 1:
        raise RuntimeError(
            f"CURRENT_GENERATION_INVALID: role={role} matching_generations={len(current)}"
        )
    return current[0]


def request_profile(profile):
    outbound = next(item for item in profile["outbounds"] if item["type"] == "trojan")
    addresses = json.loads((INPUTS / "addresses.json").read_text())
    tls = ssl.create_default_context()
    with socket.create_connection((outbound["server"], outbound["server_port"]), timeout=5) as raw:
        with tls.wrap_socket(raw, server_hostname=outbound["tls"]["server_name"]) as stream:
            stream.sendall(
                hashlib.sha224(outbound["password"].encode()).hexdigest().encode()
                + b"\r\n\x01\x01"
                + socket.inet_aton(addresses["target"])
                + (8080).to_bytes(2, "big")
                + b"\r\n"
                + b"GET / HTTP/1.0\r\nHost: foreign.lab\r\n\r\n"
            )
            response = b""
            while len(response) < 65536:
                block = stream.recv(4096)
                if not block:
                    break
                response += block
    result = json.loads(response.split(b"\r\n\r\n", 1)[1])
    assert result == {"target": "foreign", "peer": addresses["exit"]}
    return result


def entry():
    return request_profile(json.loads((CONTROL / "subscription-profile.json").read_text()))


def denied(profile):
    try:
        request_profile(profile)
    except (OSError, ValueError, IndexError):
        return True
    raise AssertionError("revoked or rotated password still reached the target")


def upgrade_cycle(role):
    if role == "ingress":
        return ingress_upgrade_cycle()
    predecessor = current_state(role)["id"]
    successor = apply(role)
    assert successor["id"] != predecessor
    third = apply(role)
    assert third["id"] not in {predecessor, successor["id"]}
    assert cli("rollback", third["id"])["state"] == "ROLLED_BACK"
    assert current_state(role)["id"] == successor["id"]
    result = cli("rollback", successor["id"])
    assert result["state"] == "ROLLED_BACK"
    restored = current_state(role)
    assert restored["id"] == predecessor and restored["state"] == "ACTIVE"
    return {
        "predecessor": predecessor,
        "successor": successor["id"],
        "third": third["id"],
        "both_predecessors_restored_active": True,
    }


def ingress_upgrade_cycle():
    predecessor = current_state("ingress")["id"]
    config_path, probes_path = CONTROL / "config.yaml", CONTROL / "probes.json"
    config_text, probes_text = config_path.read_text(), probes_path.read_text()
    original_profile = json.loads((CONTROL / "subscription-profile.json").read_text())

    def configure(port):
        config = yaml.safe_load(config_text)
        config["entry"]["endpoint"] = (
            json.loads((INPUTS / "addresses.json").read_text())["ingress"] + f":{port}"
        )
        owned_write(config_path, yaml.safe_dump(config, sort_keys=False))
        probes = json.loads(probes_text)
        next(item for item in probes if item["layer"] == "access")["port"] = port
        owned_write(probes_path, json.dumps(probes))
        profile = json.loads(json.dumps(original_profile))
        next(item for item in profile["outbounds"] if item["type"] == "trojan")["server_port"] = (
            port
        )
        return profile

    try:
        second_profile = configure(8443)
        successor = apply("ingress", monitor_previous=predecessor, test_profile=second_profile)
        denied(original_profile)
        third_profile = configure(9443)
        third = apply("ingress", monitor_previous=successor["id"], test_profile=third_profile)
        denied(second_profile)
        assert successor["revision"] != third["revision"]
        assert cli("rollback", third["id"])["state"] == "ROLLED_BACK"
        assert current_state("ingress")["id"] == successor["id"]
        request_profile(second_profile)
        denied(third_profile)
        assert cli("rollback", successor["id"])["state"] == "ROLLED_BACK"
        assert current_state("ingress")["id"] == predecessor
        request_profile(original_profile)
        denied(second_profile)
        return {
            "predecessor": predecessor,
            "successor": successor["id"],
            "third": third["id"],
            "listener_sequence": [443, 8443, 9443, 8443, 443],
            "stale_monitor_preserved_unconfirmed_listener": True,
            "both_predecessors_restored_authenticated_path": True,
            "replaced_listeners_closed": True,
        }
    finally:
        owned_write(config_path, config_text)
        owned_write(probes_path, probes_text)


def fail_component(unit):
    command(["systemctl", "stop", unit])
    monitor()
    assert current_state("ingress")["state"] == "DEGRADED"
    denied(json.loads((CONTROL / "subscription-profile.json").read_text()))
    return {"status": "DEGRADED", "protected_request_blocked": True}


def resume_component(unit):
    command(["systemctl", "start", unit])
    for _ in range(6):
        monitor()
        if current_state("ingress")["state"] == "ACTIVE":
            return {"status": "ACTIVE", "target": entry()}
        time.sleep(0.5)
    raise AssertionError("monitor did not recover the authenticated protected path")


def recover(role):
    before = current_state(role)["id"]
    units = (
        [
            "wayvia-ingress.service",
            "wayvia-bridge.service",
            "wayvia-carrier.service",
            "wayvia-vpn-client.service",
        ]
        if role == "ingress"
        else ["wayvia-exit.service", "wayvia-vpn-server.service"]
    )
    command(["systemctl", "stop", "wayvia-agent.service", *units])
    command(["systemctl", "restart", "wayvia-recover.service"])
    command(["systemctl", "start", "wayvia-agent.service", "wayvia-api.service"])
    wait_control_ready()
    restored = current_state(role)
    assert restored["id"] == before and restored["state"] == "ACTIVE"
    return {"status": "ACTIVE", "same_generation": True, "independent_systemd_recovery": True}


def reinstall(role):
    from wayvia.agent.models import UNITS

    before = current_state(role)["id"]
    device_ids = [item["id"] for item in cli("device", "list")["devices"]]
    command(
        [
            "systemctl",
            "stop",
            "wayvia-monitor.timer",
            "wayvia-monitor.service",
            "wayvia-api.service",
            "wayvia-agent.service",
            "wayvia-recover.service",
            *sorted(UNITS),
        ],
        accepted=(0, 5),
    )
    software_install()
    command(
        [
            "systemctl",
            "start",
            "wayvia-recover.service",
            "wayvia-agent.service",
            "wayvia-api.service",
        ]
    )
    wait_control_ready()
    restored = current_state(role)
    assert restored["id"] == before and restored["state"] == "ACTIVE"
    assert [item["id"] for item in cli("device", "list")["devices"]] == device_ids
    return {
        "same_generation": True,
        "control_data_preserved": True,
        "locked_runtime_reinstalled": True,
    }


def permissions():
    device_id = json.loads((CONTROL / "lab-device.json").read_text())["id"]
    previous = json.loads((CONTROL / "subscription-profile.json").read_text())
    rotated = api(f"/api/v1/devices/{device_id}/rotate", method="POST")
    assert rotated["status"] == "active"
    denied(previous)
    subscription = json.loads(Path("/var/lib/wayvia/lab-subscription.json").read_text())
    url = "http://127.0.0.1:8787/subscriptions/" + subscription["token"] + "/sing-box"
    with urllib.request.urlopen(url, timeout=10) as response:
        renewed = json.load(response)
    request_profile(renewed)
    api("/api/v1/subscriptions/" + subscription["id"], method="DELETE")
    try:
        urllib.request.urlopen(url, timeout=10)
    except urllib.error.HTTPError:
        pass
    else:
        raise AssertionError("revoked subscription still returned a profile")
    request_profile(renewed)  # Link revocation does not revoke the already imported device.
    from wayvia.agent.models import Plan

    stale = Plan.model_validate_json(make_plan("ingress").read_text())
    revoked = api(f"/api/v1/devices/{device_id}/revoke", method="POST")
    assert revoked["status"] == "revoked"
    denied(renewed)
    fresh = Plan.model_validate_json(make_plan("ingress").read_text())
    stale = stale.model_copy(update={"baseline": fresh.baseline}).seal()
    stale_path = CONTROL / "pre-revocation-plan.json"
    owned_write(stale_path, stale.model_dump_json())
    applied = cli("apply", str(stale_path), accepted=(0, 6))
    assert applied["state"] == "AWAITING_CONFIRM", applied
    cli("confirm", applied["id"])
    denied(renewed)
    assert cli("rollback", applied["id"])["state"] == "ROLLED_BACK"
    denied(renewed)
    return {
        "rotation_rejected_old_password": True,
        "new_password_accepted": True,
        "link_revoke_preserved_device": True,
        "device_revoke_rejected_access": True,
        "pre_revocation_plan_cannot_restore_access": True,
        "rollback_cannot_restore_revoked_access": True,
    }


def publication():
    cli("device", "reconcile")
    monitor()  # Reconciliation restarts ingress and invalidates prior path evidence.
    owned_write(CONTROL / "exit-status.json", (INPUTS / "exit-status.json").read_text())
    published = cli("config", "publish", "--exit-status", str(CONTROL / "exit-status.json"))
    from wayvia.config import load_config

    assert published["status"] == "published"
    assert published["published_revision"] == load_config(CONTROL / "config.yaml").revision
    device_id = json.loads((CONTROL / "lab-device.json").read_text())["id"]
    subscription = api(
        "/api/v1/subscriptions",
        method="POST",
        payload={"device_id": device_id, "format": "sing-box"},
    )
    token = subscription["token"]
    with urllib.request.urlopen(
        "http://127.0.0.1:8787/subscriptions/" + token + "/sing-box", timeout=30
    ) as response:
        assert "no-store" in response.headers["Cache-Control"]
        profile = json.load(response)
    owned_write(CONTROL / "subscription-profile.json", json.dumps(profile))
    Path("/var/lib/wayvia/lab-subscription.json").write_text(json.dumps(subscription))
    return {
        "status": "published_and_fetched",
        "revision": published["published_revision"],
        "no_store": True,
    }


def uninstall():
    command(["systemctl", "stop", "wayvia-monitor.timer", "wayvia-monitor.service"])
    for _ in range(20):
        rows = cli("status", "--json")["agent"]
        current = next(
            (row for row in rows if row["state"] in {"ACTIVE", "DEGRADED", "AWAITING_CONFIRM"}),
            None,
        )
        if current is None:
            break
        cli("rollback", current["id"])
    else:
        raise AssertionError("deployment history did not roll back completely")
    from wayvia.agent.models import UNITS

    command(
        [
            "systemctl",
            "stop",
            "wayvia-monitor.timer",
            "wayvia-monitor.service",
            "wayvia-api.service",
            "wayvia-agent.service",
            "wayvia-recover.service",
            *sorted(UNITS),
        ],
        accepted=(0, 5),
    )
    result = command(["python3.12", "/workspace/deploy/install.py", "--system", "--uninstall"])
    data = json.loads(result.stdout)
    assert data["status"] == "uninstalled_data_retained"
    assert not Path("/usr/local/bin/sing-box").exists()
    assert not Path("/etc/systemd/system/wayvia-api.service").exists()
    assert (
        command(["ip", "-j", "route", "show", "default"]).stdout
        == Path("/var/lib/wayvia/lab-default-route.json").read_text()
    )
    namespaces = command(["ip", "netns", "list"]).stdout
    assert "unrelated-lab" in namespaces and "wayvia-egress" not in namespaces
    command(["nft", "list", "table", "inet", "unrelated_lab"])
    for table in ("wayvia_guard", "wayvia_exit"):
        assert command(["nft", "list", "table", "inet", table], accepted=(0, 1)).returncode != 0
    assert command(["ip", "link", "show", "wayvia-host"], accepted=(0, 1)).returncode != 0
    assert command(["ip", "link", "show", "tun0"], accepted=(0, 1)).returncode != 0
    return {
        "status": "uninstalled",
        "control_data_retained": (CONTROL / "metadata.db").exists(),
        "default_route_preserved": True,
        "unrelated_network_resources_preserved": True,
        "owned_network_resources_removed": True,
    }


def diagnostics():
    rows = []
    for path in Path("/var/lib/wayvia/agent").glob("*.json"):
        data = json.loads(path.read_text())
        if "steps" in data:
            rows.append(
                {
                    "id": data["id"],
                    "state": data["state"],
                    "apply_error": data.get("apply_error"),
                    "apply_error_details": data.get("apply_error_details", {}),
                    "recovery_failures": data.get("recovery_failures", []),
                    "steps": [
                        {
                            "kind": item["action"]["kind"],
                            "resource": item["action"]["resource"],
                            "restored": item["restored"],
                        }
                        for item in data["steps"]
                    ],
                }
            )
    logs = command(
        [
            "journalctl",
            "--no-pager",
            "-n",
            "120",
            "-u",
            "wayvia-agent.service",
            "-u",
            "wayvia-vpn-server.service",
            "-u",
            "wayvia-exit.service",
            "-u",
            "wayvia-recover.service",
            "-u",
            "wayvia-carrier.service",
            "-u",
            "wayvia-vpn-client.service",
            "-u",
            "wayvia-ingress.service",
            "-u",
            "wayvia-bridge.service",
        ],
        accepted=(0, 1),
    ).stdout
    return {"status": cli("status", "--json"), "actions": rows, "service_log": logs}


def main():
    if os.environ.get("WAYVIA_TRANSACTION_LAB") != "1":
        raise RuntimeError("explicit disposable transaction lab required")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=[
            "install",
            "device",
            "apply",
            "monitor",
            "publication",
            "entry",
            "upgrade",
            "fail-vpn",
            "resume-vpn",
            "fail-carrier",
            "resume-carrier",
            "recover",
            "reinstall",
            "permissions",
            "uninstall",
            "status",
            "diagnostics",
        ],
    )
    parser.add_argument("--role", choices=["ingress", "exit"], default="ingress")
    args = parser.parse_args()
    result = {
        "install": lambda: install(args.role),
        "device": device,
        "apply": lambda: apply(args.role),
        "monitor": monitor,
        "publication": publication,
        "entry": entry,
        "upgrade": lambda: upgrade_cycle(args.role),
        "fail-vpn": lambda: fail_component("wayvia-vpn-client.service"),
        "resume-vpn": lambda: resume_component("wayvia-vpn-client.service"),
        "fail-carrier": lambda: fail_component("wayvia-carrier.service"),
        "resume-carrier": lambda: resume_component("wayvia-carrier.service"),
        "recover": lambda: recover(args.role),
        "reinstall": lambda: reinstall(args.role),
        "permissions": permissions,
        "uninstall": uninstall,
        "status": lambda: cli("status", "--json"),
        "diagnostics": diagnostics,
    }[args.action]()
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
        sys.exit(1)
