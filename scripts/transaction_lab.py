#!/usr/bin/env python3
"""Installed dual-role transactions in owned, offline, disposable systemd containers."""

import argparse
import ipaddress
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from wayvia.config import WayviaConfig, example_config

ROOT = Path(__file__).resolve().parents[1]
NETWORK = "wayvia-transaction-disposable"
NAMES = {role: "wayvia-transaction-" + role for role in ("ingress", "exit", "target")}
LABEL = "org.wayvia.disposable=transaction"
IMAGE = "wayvia-transaction:locked"


def command(argv, *, timeout=300, check=True):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(
            f"{Path(argv[0]).name} failed ({result.returncode}): {result.stderr[-2000:]}"
        )
    return result


def clean():
    failures = []
    for name in NAMES.values():
        result = command(["docker", "inspect", name], check=False)
        if result.returncode:
            continue
        data = json.loads(result.stdout)[0]
        if (data["Config"].get("Labels") or {}).get("org.wayvia.disposable") != "transaction":
            failures.append("refusing to remove an unowned transaction container: " + name)
            continue
        result = command(["docker", "rm", "-f", name], check=False)
        if result.returncode:
            failures.append("could not remove owned transaction container: " + name)
    result = command(["docker", "network", "inspect", NETWORK], check=False)
    if result.returncode == 0:
        data = json.loads(result.stdout)[0]
        if (data.get("Labels") or {}).get("org.wayvia.disposable") != "transaction":
            failures.append("refusing to remove an unowned transaction network")
        elif command(["docker", "network", "rm", NETWORK], check=False).returncode:
            failures.append("could not remove owned transaction network")
    if failures:
        raise RuntimeError("; ".join(failures))


def require_absent():
    for kind, name in [*(("container", name) for name in NAMES.values()), ("network", NETWORK)]:
        if command(["docker", kind, "inspect", name], check=False).returncode == 0:
            raise RuntimeError(
                "transaction lab resource already exists; inspect before cleanup: " + name
            )


def build():
    architecture = command(["docker", "info", "--format", "{{.Architecture}}"]).stdout.strip()
    architecture = {"aarch64": "arm64", "x86_64": "amd64"}.get(architecture, architecture)
    if architecture not in {"arm64", "amd64"}:
        raise RuntimeError("unsupported transaction lab architecture")
    with tempfile.TemporaryDirectory(prefix="wayvia-transaction-build-") as temporary:
        context = Path(temporary)
        for filename in ("pyproject.toml", "uv.lock"):
            shutil.copyfile(ROOT / filename, context / filename)
        for directory in ("src", "deploy", "tests/lab"):
            shutil.copytree(
                ROOT / directory,
                context / directory,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        command(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(context / "dist"),
                str(ROOT),
            ]
        )
        assets = json.loads((ROOT / "deploy/lab/components.lock.json").read_text())["components"]
        (context / "components").mkdir()
        for component in assets.values():
            name = component["assets"][architecture]["name"]
            shutil.copyfile(
                ROOT / ".cache/components" / architecture / name, context / "components" / name
            )
        command(
            [
                "docker",
                "build",
                "-t",
                IMAGE,
                "-f",
                str(ROOT / "deploy/lab/Dockerfile.transaction"),
                str(context),
            ],
            timeout=600,
        )


def certificates(directory):
    directory.mkdir()
    key, certificate = directory / "ca.key", directory / "vpn-ca.crt"
    command(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=Wayvia transaction lab CA",
        ]
    )
    for name, identity, usage in [
        ("entry-tls", "entry.lab", "serverAuth"),
        ("carrier-tls", "carrier.lab", "serverAuth"),
        ("vpn-server", "vpn-server.lab", "serverAuth"),
        ("vpn-client", "vpn-client.lab", "clientAuth"),
    ]:
        command(
            [
                "openssl",
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(directory / (name + ".key")),
                "-out",
                str(directory / (name + ".csr")),
                "-subj",
                "/CN=" + identity,
            ]
        )
        extension = directory / (name + ".ext")
        extension.write_text(
            "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage="
            + usage
            + "\nsubjectAltName=DNS:"
            + identity
            + "\n"
        )
        command(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(directory / (name + ".csr")),
                "-CA",
                str(certificate),
                "-CAkey",
                str(key),
                "-CAcreateserial",
                "-out",
                str(directory / (name + ".crt")),
                "-days",
                "1",
                "-extfile",
                str(extension),
            ]
        )
    key.unlink()  # Signing is complete; only temporary leaf credentials enter containers.
    for path in directory.iterdir():
        path.chmod(0o600)


def inputs(directory, addresses):
    data = example_config()
    data["deployment"]["mode"] = "lab"
    data["policy"]["domestic_domain_rules_ref"] = None
    data["entry"].update(
        endpoint=addresses["ingress"] + ":443", server_name="entry.lab", listen="0.0.0.0"
    )
    data["carrier"].update(
        endpoint=addresses["exit"] + ":443", server_name="carrier.lab", listen="0.0.0.0"
    )
    data["vpn"].update(subnet="10.77.0.0/24", server_name="vpn-server.lab")
    data["rulesets"] = {
        "domestic-rules": {
            "source": "isolated transaction lab",
            "version": "transaction-1",
            "domains": ["domestic.lab"],
            "cidrs": ["192.0.2.0/24"],
        },
        "local-rules": {
            "source": "isolated loopback",
            "version": "transaction-1",
            "domains": ["localhost"],
            "cidrs": ["127.0.0.0/8", "::1/128"],
        },
    }
    for resolver in data["dns"]["resolvers"].values():
        resolver["address"] = addresses["target"]
    config = WayviaConfig.model_validate(data)
    (directory / "config.yaml").write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    )
    (directory / "carrier-password").write_text(secrets.token_urlsafe(32))
    (directory / "addresses.json").write_text(json.dumps(addresses))
    certificates(directory / "certificates")
    expected = json.dumps({"target": "foreign", "peer": addresses["exit"]})
    url = "http://" + addresses["target"] + ":8080"
    for role in ("ingress", "exit"):
        ns = {"namespace": "wayvia-egress"} if role == "ingress" else {}
        probes = [
            {
                "layer": "access",
                "kind": "trojan_http" if role == "ingress" else "trojan_vpn",
                "target": "127.0.0.1",
                "port": 443,
                **({"url": url, "expected": expected} if role == "ingress" else {}),
            },
            {
                "layer": "carrier",
                "kind": "vpn_handshake",
                "target": "10.89.0.1" if ns else "127.0.0.1",
                "port": 1080 if ns else 1194,
                **ns,
            },
            {
                "layer": "vpn",
                "kind": "vpn_session",
                "target": "10.77.0.1" if ns else "10.77.0.2",
                "expected": "tun0",
                **ns,
            },
            {"layer": "egress", "kind": "http", "target": url, "expected": expected, **ns},
            {
                "layer": "dns",
                "kind": "dns",
                "target": addresses["target"],
                "port": 53,
                "expected": "foreign.lab",
                **ns,
            },
            {"layer": "ipv6", "kind": "ipv6_guard", **ns},
            {"layer": "return", "kind": "route", "target": addresses["target"], "expected": "eth0"},
        ]
        (directory / (role + "-probes.json")).write_text(json.dumps(probes))
    for path in directory.iterdir():
        if path.is_file():
            path.chmod(0o600)


def action(role, name):
    print(f"transaction lab: {role} {name}", file=sys.stderr, flush=True)
    python = (
        "/opt/wayvia-lab-python/bin/python" if name == "install" else "/opt/wayvia/.venv/bin/python"
    )
    result = command(
        [
            "docker",
            "exec",
            "-e",
            "WAYVIA_TRANSACTION_LAB=1",
            NAMES[role],
            python,
            "/workspace/tests/lab/transaction_probe.py",
            name,
            "--role",
            role,
        ]
    )
    return json.loads(result.stdout)


def run(output):
    report = {
        "status": "initializing",
        "simulated": False,
        "isolation": "owned_internal_docker_network_private_cgroups",
        "cases": [],
    }
    (ROOT / ".cache").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="wayvia-transaction-inputs-", dir=ROOT / ".cache"
    ) as temporary:
        directory = Path(temporary)
        started = False
        try:
            require_absent()
            started = True
            command(["docker", "network", "create", "--internal", "--label", LABEL, NETWORK])
            network = json.loads(command(["docker", "network", "inspect", NETWORK]).stdout)[0]
            subnet = ipaddress.ip_network(network["IPAM"]["Config"][0]["Subnet"])
            addresses = {
                role: str(subnet.network_address + index)
                for index, role in enumerate(NAMES, start=2)
            }
            inputs(directory, addresses)
            for role in ("ingress", "exit"):
                command(
                    [
                        "docker",
                        "run",
                        "--detach",
                        "--rm",
                        "--name",
                        NAMES[role],
                        "--label",
                        LABEL,
                        "--privileged",
                        "--network",
                        NETWORK,
                        "--ip",
                        addresses[role],
                        "--cgroupns",
                        "private",
                        "--tmpfs",
                        "/run",
                        "--tmpfs",
                        "/run/lock",
                        "--tmpfs",
                        "/tmp",
                        "-v",
                        str(directory) + ":/inputs:ro",
                        "-v",
                        str(ROOT / "tests/lab") + ":/workspace/tests/lab:ro",
                        IMAGE,
                    ]
                )
                for _ in range(100):
                    state = command(
                        ["docker", "exec", NAMES[role], "systemctl", "is-system-running"],
                        check=False,
                    )
                    if state.stdout.strip() in {"running", "degraded"}:
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("systemd did not become ready")
                assert (
                    command(["docker", "exec", NAMES[role], "cat", "/proc/1/comm"]).stdout.strip()
                    == "systemd"
                )
            command(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--rm",
                    "--name",
                    NAMES["target"],
                    "--label",
                    LABEL,
                    "--network",
                    NETWORK,
                    "--ip",
                    addresses["target"],
                    "--entrypoint",
                    "python3",
                    "wayvia-lab:locked",
                    "/opt/wayvia-lab/target.py",
                    "foreign",
                    addresses["target"],
                    "/tmp/requests.jsonl",
                ]
            )
            for role in ("exit", "ingress"):
                report["cases"].append(
                    {"name": role + "_system_install", "result": action(role, "install")}
                )
            report["cases"].append(
                {"name": "api_pending_device", "result": action("ingress", "device")}
            )
            for role in ("exit", "ingress"):
                report["cases"].append(
                    {"name": role + "_apply_confirm", "result": action(role, "apply")}
                )
            exit_status = action("exit", "monitor")
            (directory / "exit-status.json").write_text(json.dumps(exit_status))
            # The public status is redacted; allow the installed service user to read it.
            (directory / "exit-status.json").chmod(0o644)
            action("ingress", "monitor")
            report["cases"].append(
                {"name": "confirmed_publication", "result": action("ingress", "publication")}
            )
            for role, name, label in [
                ("ingress", "entry", "subscribed_entry_to_exit"),
                ("exit", "upgrade", "exit_successive_generation_rollback"),
                ("ingress", "upgrade", "ingress_successive_generation_rollback"),
                ("ingress", "entry", "restored_generation_entry"),
                ("ingress", "fail-vpn", "vpn_failure_closed"),
                ("ingress", "resume-vpn", "vpn_authenticated_recovery"),
                ("ingress", "fail-carrier", "carrier_failure_closed"),
                ("ingress", "resume-carrier", "carrier_authenticated_recovery"),
                ("exit", "recover", "exit_independent_recovery"),
                ("ingress", "recover", "ingress_independent_recovery"),
                ("ingress", "entry", "recovered_generation_entry"),
                ("exit", "reinstall", "exit_system_software_reinstall"),
                ("ingress", "reinstall", "ingress_system_software_reinstall"),
                ("ingress", "entry", "reinstalled_software_entry"),
                ("ingress", "permissions", "device_subscription_revocation"),
                ("ingress", "uninstall", "ingress_uninstall"),
                ("exit", "uninstall", "exit_uninstall"),
            ]:
                report["cases"].append({"name": label, "result": action(role, name)})
            report["status"] = "passed"
        except Exception as error:
            report.update(status="failed", error=str(error))
            report["diagnostics"] = {}
            for role in ("exit", "ingress"):
                try:
                    report["diagnostics"][role] = action(role, "diagnostics")
                except Exception:
                    report["diagnostics"][role] = {"status": "unavailable"}
            print("TRANSACTION LAB FAILED: " + str(error), file=sys.stderr)
        finally:
            if started:
                try:
                    clean()
                except Exception as error:
                    report.update(status="failed", cleanup_error=str(error))
            output.mkdir(parents=True, exist_ok=True)
            (output / "transaction-result.json").write_text(json.dumps(report, indent=2) + "\n")
        return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/validation/latest-lab")
    args = parser.parse_args()
    if args.clean:
        clean()
        return 0
    if not args.no_build:
        build()
    return run(args.output)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print("TRANSACTION LAB UNAVAILABLE: " + str(error), file=sys.stderr)
        sys.exit(4)
