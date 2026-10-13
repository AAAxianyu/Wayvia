"""Real mihomo DNS precedence checks, restricted to explicit disposable containers.

The production renderer supplies DNS/rules. This focused DNS-only experiment
disables TUN and mixed ingress; it does not claim mobile/TUN path acceptance.
"""

import ipaddress
import json
import os
import platform
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path

import yaml

from wayvia.config import WayviaConfig, example_config
from wayvia.render import ProfileRenderer


class DNSHandler(socketserver.BaseRequestHandler):
    def handle(self):
        packet, connection = self.request
        if len(packet) < 17:
            return
        offset, labels = 12, []
        while offset < len(packet) and packet[offset]:
            length = packet[offset]
            offset += 1
            if length > 63 or offset + length > len(packet):
                return
            labels.append(packet[offset : offset + length].decode("ascii"))
            offset += length
        if offset + 5 > len(packet):
            return
        question = packet[12 : offset + 5]
        self.server.queries.append(".".join(labels))
        answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 0, 4) + self.server.answer
        reply = packet[:2] + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0) + question + answer
        connection.sendto(reply, self.client_address)


def resolver(stack, address):
    server = socketserver.ThreadingUDPServer(("127.0.0.1", 0), DNSHandler)
    server.answer = ipaddress.ip_address(address).packed
    server.queries = []
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def close():
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)

    stack.callback(close)
    return server


def query(port, domain):
    question = b"".join(bytes([len(label)]) + label.encode() for label in domain.split("."))
    request_id = os.urandom(2)
    packet = request_id + struct.pack("!HHHHH", 0x0100, 1, 0, 0, 0) + question + b"\0\0\1\0\1"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(0.5)
        client.sendto(packet, ("127.0.0.1", port))
        reply = client.recv(4096)
    if reply[:2] != request_id or len(reply) < 16 or struct.unpack("!H", reply[6:8])[0] != 1:
        raise RuntimeError("Unexpected DNS response")
    return str(ipaddress.ip_address(reply[-4:]))


def run():
    if (
        platform.system() != "Linux"
        or not Path("/.dockerenv").exists()
        or os.environ.get("WAYVIA_LAB_DISPOSABLE") != "1"
    ):
        raise RuntimeError(
            "ENVIRONMENT_UNSUPPORTED: explicitly disposable Linux container required"
        )
    binary = shutil.which("mihomo")
    if not binary:
        raise RuntimeError("COMPONENT_MISSING: pinned mihomo required")
    version = subprocess.check_output([binary, "-v"], text=True).strip()
    if "1.19.32" not in version:
        raise RuntimeError("VERSION_MISMATCH: mihomo 1.19.32 required")
    with ExitStack() as stack:
        internal = resolver(stack, "192.0.2.10")
        domestic = resolver(stack, "192.0.2.20")
        bootstrap = resolver(stack, "192.0.2.30")
        servers = {"internal": internal, "domestic": domestic, "bootstrap": bootstrap}
        config = example_config()
        config["rulesets"]["local-rules"] = {
            "source": "isolated DNS test",
            "version": "1",
            "domains": ["overlap.example", "campus.example", "child.priority.example"],
        }
        config["rulesets"]["domestic-rules"] = {
            "source": "isolated DNS test",
            "version": "1",
            "domains": [
                "overlap.example",
                "child.campus.example",
                "priority.example",
                "entry.wayvia.example",
            ],
        }
        config["policy"]["domestic_domain_rules_ref"] = None
        for name, server in servers.items():
            config["dns"]["resolvers"][name + "-dns"] = {
                "address": "127.0.0.1",
                "port": server.server_address[1],
                "protocol": "udp",
            }
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        profile = ProfileRenderer(WayviaConfig.model_validate(config))._mihomo(
            "isolated-unused-device-password"
        )
        profile["tun"]["enable"] = False
        profile["mixed-port"] = 0
        profile["dns"]["listen"] = f"127.0.0.1:{port}"
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="wayvia-mihomo-dns-")))
        path = root / "profile.yaml"
        path.write_text(yaml.safe_dump(profile, sort_keys=False))
        log = stack.enter_context((root / "core.log").open("w+"))
        process = subprocess.Popen(
            [binary, "-d", str(root), "-f", str(path)], stdout=log, stderr=log
        )

        def stop():
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

        stack.callback(stop)
        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None:
                log.seek(0)
                raise RuntimeError("mihomo exited: " + log.read()[-2000:])
            try:
                assert query(port, "readiness.campus.example") == "192.0.2.10"
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("mihomo DNS listener readiness timeout") from None
                time.sleep(0.1)
        cases = [
            ("same_suffix", "sso.overlap.example", "internal", "192.0.2.10"),
            ("earlier_parent", "sso.child.campus.example", "internal", "192.0.2.10"),
            ("earlier_child", "sso.child.priority.example", "internal", "192.0.2.10"),
            ("later_parent_uncovered_child", "other.priority.example", "domestic", "192.0.2.20"),
            ("bootstrap_exact_survives_suffix", "entry.wayvia.example", "bootstrap", "192.0.2.30"),
            (
                "bootstrap_does_not_cover_children",
                "child.entry.wayvia.example",
                "domestic",
                "192.0.2.20",
            ),
        ]
        observations = []
        for name, domain, expected_server, expected_address in cases:
            address = query(port, domain)
            contacted = [tag for tag, server in servers.items() if domain in server.queries]
            passed = address == expected_address and contacted == [expected_server]
            observations.append(
                {
                    "case": name,
                    "domain": domain,
                    "answer": address,
                    "resolvers_contacted": contacted,
                    "status": "passed" if passed else "failed",
                }
            )
        return {
            "status": "passed"
            if all(row["status"] == "passed" for row in observations)
            else "failed",
            "version": version,
            "scope": "production mihomo DNS policy with real local UDP resolvers; "
            "TUN disabled for DNS-only test",
            "cases": observations,
        }


if __name__ == "__main__":
    try:
        result = run()
    except Exception as error:
        result = {"status": "failed", "error": str(error)}
    Path("/evidence/mihomo-dns-result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    sys.exit(0 if result["status"] == "passed" else 1)
