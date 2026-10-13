"""Bounded protocol observations in the same network namespace as the data plane.

Secrets are read from root-owned installed artifacts, never passed on argv or returned.
The certificate-only VPN probe deliberately does not authenticate as the real client.
"""

import contextlib
import hashlib
import ipaddress
import json
import select
import shlex
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PROBE_USER = "wayvia-internal-probe"


def exact(sock, size):
    data = b""
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise OSError("closed")
        data += part
    return data


@contextlib.contextmanager
def trojan_relay(payload, config, manifest):
    """A temporary loopback SOCKS endpoint whose sole transport is authenticated Trojan."""
    inbound = next(item for item in config["inbounds"] if item["type"] == "trojan")
    role = manifest["role"]
    user = (
        next(item for item in inbound["users"] if item["name"] == PROBE_USER)
        if role == "ingress"
        else inbound["users"][0]
    )
    context = ssl.create_default_context(cafile=inbound["tls"]["certificate_path"])
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    server_name = manifest["entry_server_name" if role == "ingress" else "carrier_server_name"]
    deadline = time.monotonic() + payload["timeout"]
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(payload["timeout"])
    sockets = []
    errors = []

    def forward():
        try:
            client, _ = listener.accept()
            sockets.append(client)
            client.settimeout(max(0.01, deadline - time.monotonic()))
            version, methods = exact(client, 2)
            if version != 5 or 0 not in exact(client, methods):
                raise OSError("SOCKS negotiation failed")
            client.sendall(b"\x05\x00")
            version, command, reserved, address_type = exact(client, 4)
            if (version, command, reserved) != (5, 1, 0):
                raise OSError("SOCKS command rejected")
            if address_type == 1:
                address = exact(client, 4)
            elif address_type == 4:
                address = exact(client, 16)
            elif address_type == 3:
                length = exact(client, 1)
                address = length + exact(client, length[0])
            else:
                raise OSError("SOCKS address rejected")
            port = exact(client, 2)
            raw = socket.create_connection(
                (payload["target"], payload["port"]), max(0.01, deadline - time.monotonic())
            )
            sockets.append(raw)
            upstream = context.wrap_socket(raw, server_hostname=server_name)
            sockets.append(upstream)
            upstream.sendall(
                hashlib.sha224(user["password"].encode()).hexdigest().encode()
                + b"\r\n\x01"
                + bytes([address_type])
                + address
                + port
                + b"\r\n"
            )
            client.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            client.setblocking(False)
            upstream.setblocking(False)
            while time.monotonic() < deadline:
                ready, _, _ = select.select([client, upstream], [], [], 0.05)
                # TLS may already have decrypted bytes buffered without fd readiness.
                if upstream.pending() and upstream not in ready:
                    ready.append(upstream)
                for source in ready:
                    try:
                        block = source.recv(65536)
                    except (BlockingIOError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                        continue
                    if not block:
                        return
                    destination = upstream if source is client else client
                    destination.settimeout(max(0.01, deadline - time.monotonic()))
                    destination.sendall(block)
                    destination.setblocking(False)
        except Exception:
            errors.append("TRANSPORT_FAILED")
        finally:
            for item in sockets:
                item.close()

    thread = threading.Thread(target=forward, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
        if errors:
            raise OSError(errors[0])
    finally:
        listener.close()
        for item in sockets:
            item.close()
        thread.join(timeout=0.2)


def vpn_identity(payload, root, manifest, socks=None):
    role = manifest["role"]
    options = {}
    for line in (
        (root / ("vpn-client.conf" if role == "ingress" else "vpn-server.conf"))
        .read_text()
        .splitlines()
    ):
        words = shlex.split(line, comments=True)
        if words:
            options[words[0]] = words[1:]
    with tempfile.TemporaryDirectory(prefix="wayvia-probe-") as directory:
        auth = Path(directory) / "auth"
        auth.write_text("wayvia-readiness\nno-client-authentication\n")
        auth.chmod(0o600)
        command = [
            "openvpn",
            "--client",
            "--dev",
            "null",
            "--proto",
            "tcp-client",
            "--remote",
            "127.0.0.1",
            "1194",
            "--ca",
            options["ca"][0],
            "--remote-cert-tls",
            "server",
            "--verify-x509-name",
            manifest["vpn_server_name"],
            "name",
            "--auth-user-pass",
            str(auth),
            "--auth-nocache",
            "--route-noexec",
            "--ifconfig-noexec",
            "--script-security",
            "0",
            "--connect-timeout",
            "2",
            "--connect-retry-max",
            "1",
            "--verb",
            "3",
        ]
        if socks:
            command += ["--socks-proxy", socks[0], str(socks[1])]
        if "tls-crypt" in options:
            command += ["--tls-crypt", options["tls-crypt"][0]]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + payload["timeout"]
        output = b""
        verified = False
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select(
                    [process.stdout], [], [], min(0.1, max(0, deadline - time.monotonic()))
                )
                if ready:
                    data = process.stdout.read1(4096)
                    if not data:
                        break
                    output = (output + data)[-65536:]
                    if b"VERIFY X509NAME OK" in output and b"VERIFY OK: depth=0" in output:
                        verified = True
                        break
                if process.poll() is not None:
                    break
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=0.3)
        return {"healthy": verified, "check": "vpn_server_identity", "client_authenticated": False}


def vpn_session(payload, manifest):
    role = manifest["role"]
    name = "client" if role == "ingress" else "server"
    deadline = time.monotonic() + payload["timeout"]
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(payload["timeout"])
        connection.connect(f"/run/wayvia/vpn-{name}.sock")

        def read_response():
            data = b""
            while len(data) < 65536:
                connection.settimeout(max(0.01, deadline - time.monotonic()))
                part = connection.recv(4096)
                if not part:
                    raise OSError("incomplete management response")
                data += part
                if (
                    b"\nEND\r\n" in data
                    or b"\nEND\n" in data
                    or data.startswith(b"END\r\n")
                    or data.startswith(b"END\n")
                ):
                    lines = data.decode(errors="replace").splitlines()
                    return "\n".join(lines[: lines.index("END")])
            raise OSError("oversized management response")

        # OpenVPN can close on quit before flushing prior pipelined responses.
        connection.sendall(b"state 1\n")
        state = read_response()
        connection.sendall(b"status 3\n")
        status = read_response()
        connection.sendall(b"quit\n")
    rows = [line.split(",") for line in state.splitlines() if line.split(",")[0].isdigit()]
    latest = max(enumerate(rows), key=lambda item: (int(item[1][0]), item[0]), default=(0, []))[1]
    healthy = len(latest) >= 3 and latest[1:3] == ["CONNECTED", "SUCCESS"]
    return {
        "healthy": healthy,
        "check": "authenticated_vpn_session" if role == "ingress" else "vpn_server_initialized",
        "peer_count": status.count("CLIENT_LIST\t"),
    }


def run(payload):
    root = Path(payload["config_dir"])
    manifest = json.loads((root / "manifest.json").read_text())
    kind = payload["kind"]
    if kind == "vpn_session":
        return vpn_session(payload, manifest)
    if kind == "vpn_handshake":
        socks = (payload["target"], payload["port"]) if manifest["role"] == "ingress" else None
        return vpn_identity(payload, root, manifest, socks)
    config = json.loads(
        (root / ("ingress.json" if manifest["role"] == "ingress" else "exit.json")).read_text()
    )
    ipaddress.ip_address(payload["target"])
    with trojan_relay(payload, config, manifest) as port:
        if kind == "trojan_vpn":
            return vpn_identity(payload, root, manifest, ("127.0.0.1", port))
        result = subprocess.run(
            [
                "curl",
                "--silent",
                "--show-error",
                "--fail",
                "--noproxy",
                "",
                "--socks5-hostname",
                f"127.0.0.1:{port}",
                "--max-time",
                str(payload["timeout"]),
                "--",
                payload["url"],
            ],
            capture_output=True,
            timeout=payload["timeout"],
            check=False,
        )
        return {
            "healthy": result.returncode == 0
            and result.stdout.decode().strip() == payload["expected"],
            "check": "authenticated_ingress_business_path",
        }


def main():
    try:
        result = run(json.loads(sys.stdin.read(65536)))
    except Exception:
        result = {"healthy": False, "error": "PROTOCOL_PROBE_FAILED"}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
