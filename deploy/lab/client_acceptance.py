#!/usr/bin/env python3
"""Normal client API/selection/protocol acceptance in the network-none disposable container.

No TUN, route, firewall, server recovery or fault injection. Extra node/targets
are synthetic cache fixtures and never become a published Wayvia subscription.
"""

import copy
import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener

import yaml

from wayvia.client import GROUP, NODE

PHASE = "startup"


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run(source, ca_root):
    global PHASE
    if not Path("/.dockerenv").exists() or not Path("/generated").is_dir():
        raise RuntimeError("disposable_container_required")
    original = yaml.safe_load(Path(source).read_text())
    assert original["profile"]["store-selected"] is True
    assert original["proxy-groups"][0]["proxies"] == [NODE]
    assert original["proxy-groups"][0]["interval"] == 0
    assert original["proxy-groups"][0]["empty-fallback"] == "REJECT"
    requests = []
    payload = b"x" * 409600

    class Target(BaseHTTPRequestHandler):
        def do_HEAD(self):
            if self.path == "/generate_204":
                self.do_GET()
            else:
                self.send_response(405)
                self.end_headers()

        def do_GET(self):
            requests.append(self.path)
            if self.path == "/generate_204":
                self.send_response(204)
                self.end_headers()
            elif self.path == "/payload":
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                for start in range(0, len(payload), 4096):
                    self.wfile.write(payload[start : start + 4096])
                    self.wfile.flush()
                    time.sleep(0.01)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=target.serve_forever, daemon=True).start()
    root = Path(tempfile.mkdtemp(prefix="client-contract-"))
    api, mixed, entry = port(), port(), port()
    secret = secrets.token_urlsafe(32)
    fixture = copy.deepcopy(original)
    fixture["tun"]["enable"] = False
    fixture["dns"]["enable"] = False
    fixture["mixed-port"] = mixed
    fixture["external-controller"] = f"127.0.0.1:{api}"
    fixture["secret"] = secret
    fixture["rules"] = ["MATCH,Wayvia"]
    fixture["proxies"][0].update(server="127.0.0.1", port=entry, sni="entry.lab")
    sentinel = copy.deepcopy(fixture["proxies"][0])
    sentinel["name"] = "fixture-cache-sentinel"
    fixture["proxies"].append(sentinel)
    fixture["proxy-groups"][0]["proxies"].append(sentinel["name"])
    fixture["proxy-groups"][0]["url"] = f"http://127.0.0.1:{target.server_port}/generate_204"
    config = root / "mihomo.yaml"
    config.write_text(yaml.safe_dump(fixture, sort_keys=False, allow_unicode=True))
    config.chmod(0o600)
    server = {
        "log": {"level": "error"},
        "inbounds": [
            {
                "type": "trojan",
                "listen": "127.0.0.1",
                "listen_port": entry,
                "users": [
                    {"name": "synthetic-client", "password": fixture["proxies"][0]["password"]}
                ],
                "tls": {
                    "enabled": True,
                    "certificate_path": str(ca_root / "entry.crt"),
                    "key_path": str(ca_root / "entry.key"),
                },
            }
        ],
        "outbounds": [{"type": "direct"}],
    }
    server_path = root / "server.json"
    server_path.write_text(json.dumps(server))
    server_path.chmod(0o600)
    processes = []
    opener = build_opener(ProxyHandler({}))

    def call(path, method="GET", value=None):
        request = Request(
            f"http://127.0.0.1:{api}" + path,
            method=method,
            data=json.dumps(value).encode() if value is not None else None,
            headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"},
        )
        with opener.open(request, timeout=5) as response:
            raw = response.read(2 * 1024 * 1024)
            return json.loads(raw) if raw else None

    def start_core():
        proc = subprocess.Popen(
            ["mihomo", "-f", str(config), "-d", str(root / "core")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "SSL_CERT_FILE": str(ca_root / "ca.crt")},
        )
        processes.append(proc)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("core_start_failed")
            try:
                ready = call("/proxies/" + quote(GROUP, safe=""))
                if NODE not in ready.get("all", []):
                    time.sleep(0.05)
                    continue
                return proc
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("core_api_timeout")

    def stop(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    try:
        singbox = subprocess.Popen(
            ["sing-box", "run", "-c", str(server_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(singbox)
        PHASE = "core_start"
        core = start_core()
        PHASE = "selected_node"
        group = call("/proxies/" + quote(GROUP, safe=""))
        assert group["now"] == NODE
        PHASE = "empty_fallback"
        assert group["emptyFallback"] == "REJECT"
        PHASE = "group_test_url"
        assert group["testUrl"] == fixture["proxy-groups"][0]["url"]
        PHASE = "no_automatic_request"
        assert not requests  # No periodic or startup target request.
        PHASE = "manual_delay"
        measured = call(
            "/proxies/"
            + quote(NODE, safe="")
            + "/delay?"
            + urlencode({"url": fixture["proxy-groups"][0]["url"], "timeout": 5000})
        )
        assert type(measured["delay"]) is int and measured["delay"] >= 0
        PHASE = "payload_chain"
        connection = http.client.HTTPConnection("127.0.0.1", mixed, timeout=5)
        connection.request("GET", f"http://127.0.0.1:{target.server_port}/payload")
        response = connection.getresponse()
        assert response.status == 200
        first = response.read(4096)
        active = call("/connections")
        assert any(
            NODE in row.get("chains", []) and GROUP in row.get("chains", [])
            for row in active["connections"]
        )
        body = first + response.read(len(payload))
        connection.close()
        assert body == payload
        PHASE = "selection_reload"
        call("/proxies/" + quote(GROUP, safe=""), "PUT", {"name": sentinel["name"]})
        updated = copy.deepcopy(fixture)
        for node in updated["proxies"]:
            node["password"] = "rotated-synthetic-not-used-for-traffic"
        call(
            "/configs",
            "PUT",
            {"payload": yaml.safe_dump(updated, sort_keys=False, allow_unicode=True)},
        )
        assert call("/proxies/" + quote(GROUP, safe=""))["now"] == sentinel["name"]
        # Invalid YAML is parser validation only, not a data-plane fault experiment.
        from urllib.error import HTTPError

        try:
            call("/configs", "PUT", {"payload": "proxies: [\n"})
        except HTTPError:
            pass
        else:
            raise RuntimeError("invalid_config_accepted")
        assert call("/proxies/" + quote(GROUP, safe=""))["now"] == sentinel["name"]
        PHASE = "cache_reopen"
        # The core batches cache writes. This is a normal close, not a crash-durability claim.
        time.sleep(1.2)
        stop(core)
        core = start_core()
        assert call("/proxies/" + quote(GROUP, safe=""))["now"] == sentinel["name"]
        PHASE = "manual_request_counts"
        assert requests.count("/generate_204") == 1 and requests.count("/payload") == 1
        return {
            "component": "mihomo+sing-box",
            "artifact": "unified-client-contract",
            "exit_code": 0,
            "scope": "disposable_loopback_protocol_and_normal_config_API_cache",
            "manual_delay_via_authenticated_TLS_trojan": True,
            "payload_bytes_verified": len(payload),
            "active_chain_includes_wayvia_node": True,
            "startup_periodic_requests": 0,
            "selection_survives_normal_reload_and_core_reopen": True,
            "invalid_candidate_retains_selection": True,
            "tun_enabled": False,
            "external_network": False,
            "full_server_VPN_over_proxy_path": "not_tested",
        }
    finally:
        for process in reversed(processes):
            stop(process)
        target.shutdown()
        target.server_close()


if __name__ == "__main__":
    try:
        print(json.dumps(run(Path(sys.argv[1]), Path(sys.argv[2]))))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": "CLIENT_NATIVE_ACCEPTANCE_FAILED",
                    "phase": PHASE,
                    "error_type": type(exc).__name__,
                }
            )
        )
        raise SystemExit(5) from None
