#!/usr/bin/env python3
"""Real protocol-positive and counterfeit-endpoint checks in a disposable container."""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from wayvia.agent.probe_runtime import run as observe


def main():
    if not (
        sys.platform == "linux"
        and Path("/.dockerenv").exists()
        and os.environ.get("WAYVIA_LAB_DISPOSABLE") == "1"
    ):
        raise RuntimeError("explicit disposable environment is required")
    sys.path.insert(0, "/opt/wayvia-lab")
    import run as laboratory

    laboratory.ca()
    root = Path("/etc/wayvia")
    runtime = Path("/run/wayvia")
    runtime.mkdir(exist_ok=True)
    for name in ("manifest.json", "exit.json", "vpn-server.conf"):
        shutil.copyfile(Path("/generated/exit") / name, root / name)
    manifest = json.loads((root / "manifest.json").read_text())
    original = json.loads((root / "exit.json").read_text())
    original["inbounds"][0]["listen"] = "127.0.0.1"
    (root / "exit.json").write_text(json.dumps(original))
    output = {"status": "running", "simulated": False, "cases": []}
    processes = []
    threads = []
    listener = None
    try:
        with (laboratory.WORK / "vpn.log").open("w") as log:
            vpn = subprocess.Popen(
                [
                    "openvpn",
                    "--config",
                    str(root / "vpn-server.conf"),
                    "--management",
                    str(runtime / "vpn-server.sock"),
                    "unix",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        processes.append(vpn)
        proxy = subprocess.Popen(
            ["sing-box", "run", "-c", str(root / "exit.json")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(proxy)
        for _ in range(100):
            if "Initialization Sequence Completed" in (laboratory.WORK / "vpn.log").read_text():
                try:
                    with socket.create_connection(("127.0.0.1", 443), timeout=0.1):
                        break
                except OSError:
                    pass
            time.sleep(0.05)
        else:
            raise RuntimeError("protocol components did not initialize")

        def check(name, kind, expected):
            payload = {
                "config_dir": str(root),
                "kind": kind,
                "timeout": 3,
                "target": "127.0.0.1",
                "port": 443 if kind == "trojan_vpn" else 1194,
            }
            try:
                result = observe(payload)
            except (OSError, ValueError):
                result = {"healthy": False, "error": "PROTOCOL_REJECTED"}
            assert result["healthy"] is expected, (name, result)
            output["cases"].append({"name": name, "status": "passed", "observed": result})

        check("real_openvpn_server_identity", "vpn_handshake", True)
        check("real_openvpn_management_state", "vpn_session", True)
        check("real_trojan_authentication_and_vpn_identity", "trojan_vpn", True)
        altered = json.loads(json.dumps(original))
        altered["inbounds"][0]["users"][0]["password"] = "incorrect-probe-password" * 2
        (root / "exit.json").write_text(json.dumps(altered))
        check("wrong_trojan_credential_rejected", "trojan_vpn", False)
        (root / "exit.json").write_text(json.dumps(original))
        (root / "manifest.json").write_text(
            json.dumps({**manifest, "carrier_server_name": "wrong.lab"})
        )
        check("wrong_carrier_tls_identity_rejected", "trojan_vpn", False)
        (root / "manifest.json").write_text(
            json.dumps({**manifest, "vpn_server_name": "wrong.lab"})
        )
        check("wrong_vpn_identity_rejected", "vpn_handshake", False)
        (root / "manifest.json").write_text(json.dumps(manifest))
        proxy.terminate()
        proxy.wait(timeout=3)
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 443))
        listener.listen(1)
        listener.settimeout(4)

        def plain_endpoint():
            with listener.accept()[0] as connection:
                connection.sendall(b"HTTP/1.0 200 OK\r\n\r\nplain TCP endpoint")

        thread = threading.Thread(target=plain_endpoint, daemon=True)
        thread.start()
        threads.append(thread)
        check("plain_tcp_listener_cannot_pass_authentication", "trojan_vpn", False)
        output["status"] = "passed"
        return 0
    except Exception as error:
        output.update(status="failed", error=type(error).__name__)
        return 1
    finally:
        if listener:
            listener.close()
        for thread in threads:
            thread.join(timeout=0.2)
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        Path("/evidence/protocol-result.json").write_text(json.dumps(output, indent=2) + "\n")
        print(json.dumps(output))


if __name__ == "__main__":
    sys.exit(main())
