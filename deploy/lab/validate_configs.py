#!/usr/bin/env python3
"""Validate production-rendered artifacts with the pinned real Linux components."""

import json
import subprocess
import sys
import time
from pathlib import Path

from run import WORK, ca, command, environment


def main():
    ca()
    results = []
    for path in sorted(Path("/generated").rglob("*.json")):
        if path.name not in [
            "ingress.json",
            "carrier.json",
            "bridge.json",
            "exit.json",
            "profile-sing-box.json",
        ]:
            continue
        process = subprocess.run(
            ["sing-box", "check", "-c", str(path)], capture_output=True, text=True
        )
        results.append(
            {
                "component": "sing-box",
                "artifact": str(path.relative_to("/generated")),
                "command": ["sing-box", "check", "-c", "<artifact>"],
                "exit_code": process.returncode,
                "output": process.stdout + process.stderr,
            }
        )
        if process.returncode:
            raise RuntimeError("sing-box rejected " + str(path.relative_to("/generated")))
    profile = Path("/generated/profile-mihomo.yaml")
    process = subprocess.run(
        ["mihomo", "-t", "-f", str(profile), "-d", "/tmp/mihomo-validate"],
        capture_output=True,
        text=True,
    )
    results.append(
        {
            "component": "mihomo",
            "artifact": profile.name,
            "command": ["mihomo", "-t", "-f", "<artifact>"],
            "exit_code": process.returncode,
            "output": process.stdout + process.stderr,
        }
    )
    if process.returncode:
        raise RuntimeError("mihomo rejected profile")
    client_check = subprocess.run(
        [
            "/opt/wayvia-lab-python/bin/python",
            "/opt/wayvia-lab/client_acceptance.py",
            str(profile),
            str(WORK),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if client_check.returncode:
        result = json.loads(client_check.stdout) if client_check.stdout.strip() else {}
        raise RuntimeError(
            "unified client acceptance failed at "
            + str(result.get("phase", "startup"))
            + " ("
            + str(result.get("error_type", "unknown"))
            + ")"
        )
    results.append(json.loads(client_check.stdout))
    for role, name in [("ingress", "vpn-client.conf"), ("exit", "vpn-server.conf")]:
        namespace = "wayvia-check-" + role
        command(["ip", "netns", "add", namespace])
        command(["ip", "link", "set", "lo", "up"], ns=namespace)
        output = Path("/tmp/" + role + ".log")
        with output.open("w") as stream:
            process = subprocess.Popen(
                [
                    "ip",
                    "netns",
                    "exec",
                    namespace,
                    "openvpn",
                    "--config",
                    "/generated/" + role + "/" + name,
                ],
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
            try:
                time.sleep(2)
                text = output.read_text()
                if process.poll() is not None or any(
                    failure in text
                    for failure in ["Options error", "Cannot load", "Exiting due to fatal error"]
                ):
                    raise RuntimeError("OpenVPN rejected " + name + ": " + text)
                if role == "exit" and "Listening for incoming TCP connection" not in text:
                    raise RuntimeError("OpenVPN did not create the expected server listener")
                results.append(
                    {
                        "component": "openvpn",
                        "artifact": role + "/" + name,
                        "exit_code": None,
                        "scope": "real configuration load and isolated process startup",
                        "tunnel_established": False,
                        "output": text,
                    }
                )
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                command(["ip", "netns", "delete", namespace], check=False)
    invalid = Path("/tmp/invalid-sing-box.json")
    invalid.write_text(json.dumps({"inbounds": [{"type": "wayvia-not-a-protocol"}]}))
    process = subprocess.run(
        ["sing-box", "check", "-c", str(invalid)], capture_output=True, text=True
    )
    if process.returncode == 0:
        raise RuntimeError("sing-box accepted an unsupported protocol")
    results.append(
        {
            "component": "sing-box",
            "artifact": "deliberately invalid protocol",
            "expected_rejection": True,
            "exit_code": process.returncode,
            "output": process.stdout + process.stderr,
        }
    )
    invalid = Path("/tmp/invalid-mihomo.yaml")
    invalid.write_text("proxies:\n  - name: bad\n    type: wayvia-not-a-protocol\n")
    process = subprocess.run(
        ["mihomo", "-t", "-f", str(invalid), "-d", "/tmp/mihomo-invalid"],
        capture_output=True,
        text=True,
    )
    if process.returncode == 0:
        raise RuntimeError("mihomo accepted an unsupported protocol")
    results.append(
        {
            "component": "mihomo",
            "artifact": "deliberately invalid protocol",
            "expected_rejection": True,
            "exit_code": process.returncode,
            "output": process.stdout + process.stderr,
        }
    )
    return results


if __name__ == "__main__":
    report = {"status": "running", "results": []}
    try:
        report["results"] = main()
        report["environment"] = environment()
        report["status"] = "passed"
        for binary, flag in [
            ("sing-box", "version"),
            ("mihomo", "-v"),
            ("v2raya", "--version"),
            ("v2raya_core", "version"),
            ("openvpn", "--version"),
        ]:
            process = subprocess.run([binary, flag], capture_output=True, text=True)
            report.setdefault("versions", {})[binary] = process.stdout.splitlines()[0]
        print(json.dumps({"status": "passed", "artifacts_validated": len(report["results"])}))
    except Exception as error:
        report.update(status="failed", error=str(error))
        print("CONFIG VALIDATION FAILED: " + str(error), file=sys.stderr)
    serialized = json.dumps(report, indent=2) + "\n"
    credentials = json.loads(Path("/generated/lab-credentials.json").read_text())
    for secret in credentials.values():
        serialized = serialized.replace(secret, "[redacted]")
    Path("/evidence/config-result.json").write_text(serialized)
    sys.exit(0 if report["status"] == "passed" else 1)
