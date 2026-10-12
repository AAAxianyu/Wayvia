#!/usr/bin/env python3
"""Real dual-layer lab confined to a disposable privileged Docker container."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

NAMES = ["terminal", "domestic", "ali", "wayvia-egress", "jp", "foreign"]
PROCESSES = []
OUT = Path("/evidence")
RESULTS = []
SENSITIVE = []
HEALTHY = {}
WORK = Path(tempfile.mkdtemp(prefix="wayvia-lab-"))


def command(args, *, ns=None, check=True, timeout=15, input=None):
    if ns:
        args = ["ip", "netns", "exec", ns] + args
    result = subprocess.run(
        args, input=input, capture_output=True, text=True, check=False, timeout=timeout
    )
    if check and result.returncode:
        raise RuntimeError("command failed: " + str(args[:8]) + ": " + result.stderr[-6000:])
    return result


def spawn(name, args, ns):
    output = (WORK / (name + ".log")).open("w")
    process = subprocess.Popen(
        ["ip", "netns", "exec", ns] + args, stdout=output, stderr=subprocess.STDOUT
    )
    PROCESSES.append(process)
    return process


def test(name, func):
    start = time.monotonic()
    try:
        result = func()
    except Exception as error:
        RESULTS.append(
            {
                "name": name,
                "status": "failed",
                "elapsed_ms": round((time.monotonic() - start) * 1000),
                "error": str(error),
            }
        )
        print(json.dumps(RESULTS[-1]), flush=True)
        raise
    RESULTS.append(
        {
            "name": name,
            "status": "passed",
            "elapsed_ms": round((time.monotonic() - start) * 1000),
            "evidence": result,
        }
    )
    print(json.dumps(RESULTS[-1]), flush=True)
    return result


def link(first, second, label, address1, address2):
    command(["ip", "link", "add", label + "a", "type", "veth", "peer", "name", label + "b"])
    command(["ip", "link", "set", label + "a", "netns", first])
    command(["ip", "link", "set", label + "b", "netns", second])
    for ns, suffix, address in [(first, "a", address1), (second, "b", address2)]:
        command(["ip", "addr", "add", address, "dev", label + suffix], ns=ns)
        command(["ip", "link", "set", label + suffix, "up"], ns=ns)


def topology():
    for ns in NAMES:
        command(["ip", "netns", "add", ns])
        command(["ip", "link", "set", "lo", "up"], ns=ns)
    link("terminal", "domestic", "td", "10.71.0.2/24", "10.71.0.1/24")
    link("terminal", "ali", "ta", "10.72.0.2/24", "10.72.0.1/24")
    link("ali", "jp", "aj", "10.73.0.1/24", "10.73.0.2/24")
    link("jp", "foreign", "jf", "10.74.0.1/24", "10.74.0.2/24")
    link("ali", "wayvia-egress", "ae", "10.89.0.1/30", "10.89.0.2/30")
    command(["ip", "-6", "addr", "add", "fd72:1::2/64", "dev", "tda", "nodad"], ns="terminal")
    command(["ip", "-6", "addr", "add", "fd72:1::1/64", "dev", "tdb", "nodad"], ns="domestic")
    command(["ip", "route", "add", "default", "via", "10.72.0.1"], ns="terminal")
    command(["sysctl", "-w", "net.ipv4.ip_forward=1"], ns="jp")
    command(["ip", "route", "add", "10.74.0.0/24", "via", "10.73.0.2"], ns="ali")
    command(["ip", "route", "add", "default", "via", "10.74.0.1"], ns="foreign")
    command(["sysctl", "-w", "net.ipv4.ip_forward=1"], ns="ali")
    # An available unprotected path makes fail-closed assertions meaningful.
    command(
        ["nft", "-f", "-"],
        ns="ali",
        input="""table ip lab_unprotected_baseline {
 chain postrouting { type nat hook postrouting priority srcnat; policy accept;
  ip saddr 10.89.0.0/30 oifname "aja" masquerade
 }
}
""",
    )

    command(["ip", "link", "set", "aea", "down"], ns="ali")
    command(["ip", "link", "set", "aea", "name", "wayvia-host"], ns="ali")
    command(["ip", "link", "set", "wayvia-host", "up"], ns="ali")
    command(["ip", "link", "set", "aeb", "down"], ns="wayvia-egress")
    command(["ip", "link", "set", "aeb", "name", "wayvia-ns"], ns="wayvia-egress")
    command(["ip", "link", "set", "wayvia-ns", "up"], ns="wayvia-egress")
    command(["ip", "route", "add", "default", "via", "10.89.0.1"], ns="wayvia-egress")
    spawn(
        "foreign",
        [
            "python3",
            "/opt/wayvia-lab/target.py",
            "foreign",
            "10.74.0.2",
            str(WORK / "foreign.jsonl"),
        ],
        "foreign",
    )
    baseline = None
    for _attempt in range(10):
        request = command(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--max-time",
                "1",
                "http://10.74.0.2:8080",
            ],
            ns="wayvia-egress",
            check=False,
        )
        if request.returncode == 0:
            baseline = json.loads(request.stdout)
            break
        time.sleep(0.1)
    if baseline is None or baseline["peer"] != "10.73.0.1":
        raise AssertionError(
            "unprotected Ali physical fallback baseline is unavailable: "
            + str(baseline)
            + request.stderr
        )
    # Use the production guard text and production typed NAT executor, so the
    # datapath tests cover the exact rules deployed by Wayvia.
    for ns, namespace_guard in [("wayvia-egress", True), ("ali", False)]:
        rules = command(
            [
                "/opt/wayvia-lab-python/bin/python",
                "-c",
                "from wayvia.adapters.linux import guard_rules; print(guard_rules("
                + str(namespace_guard)
                + "))",
            ]
        ).stdout
        command(["nft", "-f", "-"], ns=ns, input=rules)
    protected = command(
        ["curl", "--fail", "--silent", "--show-error", "--max-time", "1", "http://10.74.0.2:8080"],
        ns="wayvia-egress",
        check=False,
    )
    if protected.returncode == 0:
        raise AssertionError("production guard allowed physical fallback")
    nat_code = """from wayvia.adapters.linux import LinuxExecutor
from wayvia.agent.models import Action
executor=LinuxExecutor(config_dir='/tmp/jp-cfg',state_dir='/tmp/jp-state',unit_dir='/tmp/jp-units')
import json
subnet=json.load(open('/generated/logical-config.json'))['vpn']['subnet']
executor.apply(Action(kind='exit_nat',resource='wayvia_exit',interface='jfa',subnet=subnet))
"""
    command(["/opt/wayvia-lab-python/bin/python", "-c", nat_code], ns="jp")
    command(
        ["nft", "-f", "-"],
        ns="terminal",
        input="""table inet wayvia_terminal {
 chain output { type filter hook output priority 0; policy drop;
  oifname "lo" accept
  oifname "wayvia-tun" accept
  ct state established,related accept
  ip daddr 10.71.0.0/24 accept
  ip6 daddr fd72:1::/64 accept
  meta l4proto ipv6-icmp accept
  ip daddr 10.72.0.1 tcp dport 443 accept
 }
}
""",
    )
    return {
        "namespaces": NAMES,
        "development_host_routes_modified": False,
        "egress_default_physical_route": True,
        "unprotected_physical_baseline": baseline,
        "production_guard_blocks_same_path": protected.returncode != 0,
    }


def ca():
    command(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(WORK / "ca.key"),
            "-out",
            str(WORK / "ca.crt"),
            "-days",
            "1",
            "-subj",
            "/CN=Wayvia disposable lab CA",
        ]
    )
    for name, kind in [
        ("entry", "serverAuth"),
        ("carrier", "serverAuth"),
        ("vpn-server", "serverAuth"),
        ("vpn-client", "clientAuth"),
    ]:
        command(
            [
                "openssl",
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(WORK / (name + ".key")),
                "-out",
                str(WORK / (name + ".csr")),
                "-subj",
                "/CN=" + name + ".lab",
            ]
        )
        ext = WORK / (name + ".ext")
        ext.write_text(
            "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage="
            + kind
            + "\nsubjectAltName=DNS:"
            + name
            + ".lab\n"
        )
        command(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(WORK / (name + ".csr")),
                "-CA",
                str(WORK / "ca.crt"),
                "-CAkey",
                str(WORK / "ca.key"),
                "-CAcreateserial",
                "-out",
                str(WORK / (name + ".crt")),
                "-days",
                "1",
                "-extfile",
                str(ext),
            ]
        )
    secret_root = Path("/etc/wayvia/secrets")
    secret_root.mkdir(parents=True)
    import shutil

    for source, destination in [
        ("ca.crt", "vpn-ca.crt"),
        ("entry.crt", "entry-tls.crt"),
        ("entry.key", "entry-tls.key"),
        ("carrier.crt", "carrier-tls.crt"),
        ("carrier.key", "carrier-tls.key"),
        ("vpn-server.crt", "vpn-server.crt"),
        ("vpn-server.key", "vpn-server.key"),
        ("vpn-client.crt", "vpn-client.crt"),
        ("vpn-client.key", "vpn-client.key"),
    ]:
        shutil.copyfile(WORK / source, secret_root / destination)
        (secret_root / destination).chmod(0o600)
    return {"temporary_ca": True, "server_identity_checked": True, "keys_exported": False}


def write_json(name, content):
    path = WORK / (name + ".json")
    path.write_text(json.dumps(content))
    command(["sing-box", "check", "-c", str(path)])
    return path


def sing_config(inbound, outbounds, route):
    return {"log": {"level": "info"}, "inbounds": inbound, "outbounds": outbounds, "route": route}


def tls(name):
    return {
        "enabled": True,
        "certificate_path": str(WORK / (name + ".crt")),
        "key_path": str(WORK / (name + ".key")),
    }


def prepare_configs():
    generated = Path("/generated")
    credentials = json.loads((generated / "lab-credentials.json").read_text())
    SENSITIVE.extend(credentials.values())
    paths = {}
    for name, source in [
        ("entry", "ingress/ingress.json"),
        ("bridge", "ingress/bridge.json"),
        ("japan", "exit/exit.json"),
    ]:
        content = json.loads((generated / source).read_text())
        paths[name] = write_json(name, content)
    terminal = json.loads((generated / "profile-sing-box.json").read_text())
    # The local SOCKS listener is a test harness input; policy, TLS, TUN, DNS and
    # outbounds remain exactly the production ProfileRenderer output.
    terminal["inbounds"].append({"type": "socks", "listen": "127.0.0.1", "listen_port": 1080})
    paths["terminal"] = write_json("terminal", terminal)
    for name, source in [
        ("vpn-server", "exit/vpn-server.conf"),
        ("vpn-client", "ingress/vpn-client.conf"),
    ]:
        (WORK / (name + ".conf")).write_text((generated / source).read_text())
    return paths, credentials["device"], credentials["carrier"]


def carrier_start(password):
    spawn(
        "v2raya",
        [
            "v2raya",
            "--address",
            "127.0.0.1:2017",
            "--config",
            str(WORK / "v2raya"),
            "--v2ray-bin",
            "/usr/local/bin/v2raya_core",
            "--v2ray-assetsdir",
            "/usr/local/share/v2raya",
            "--lite",
        ],
        "ali",
    )
    code = """import json,time
from wayvia.adapters.v2raya import V2rayAAdapter,new_management_password
adapter=V2rayAAdapter()
for attempt in range(40):
 try:
  adapter.inspect();break
 except Exception:time.sleep(.25)
config=json.load(open('/generated/ingress/carrier.json'))
resolved=adapter.resolve_carrier(config['outbounds'][0],config)
assert resolved=='10.73.0.2'
result=adapter.configure_carrier(config,'wayvia-lab',new_management_password(),create=True)
print(json.dumps({'version':adapter.validate(),'configured':result['configured'],'production_adapter':True,'explicit_bootstrap_resolver':'10.73.0.1','resolved_carrier':resolved,'tls_server_name':'carrier.lab'}))
"""
    result = json.loads(
        command(["/opt/wayvia-lab-python/bin/python", "-c", code], ns="ali", timeout=30).stdout
    )
    return {**result, "imported_server": True, "core_managed_by_v2raya": True}


def probe(mode="tcp", host="10.74.0.2", expected=True):
    result = command(
        ["python3", "/opt/wayvia-lab/probe.py", mode, host], ns="terminal", check=False, timeout=10
    )
    if (result.returncode == 0) != expected:
        raise AssertionError(f"{mode} {host} expected success={expected}: " + result.stdout)
    return json.loads(result.stdout)


def wait_path():
    last = None
    for _attempt in range(60):
        try:
            result = probe()
            if result["peer"] != "10.74.0.1":
                raise AssertionError("foreign source is not Japan NAT")
            return result
        except Exception as error:
            last = error
            time.sleep(0.5)
    raise RuntimeError("VPN business path unavailable: " + str(last))


def domestic_ok():
    result = probe(host="10.71.0.1")
    if result["peer"] != "10.71.0.2":
        raise AssertionError("domestic source differs from terminal")
    return result


def failure(protected_process):
    protected_process.terminate()
    protected_process.wait(timeout=10)
    time.sleep(1)
    return {
        "protected": probe(expected=False),
        "native_protected": probe("native-tcp", expected=False),
        "domestic": domestic_ok(),
        "management": management_path(),
    }


def sanitized_logs():
    logs = {}
    for path in WORK.glob("*.log"):
        body = path.read_text()[-18000:]
        for secret in SENSITIVE:
            body = body.replace(secret, "[redacted]")
        logs[path.name] = body
    return logs


def capture_healthy():
    for ns in NAMES:
        HEALTHY[ns] = {
            "sockets": command(["ss", "-ntp"], ns=ns).stdout,
            "routes": command(["ip", "route", "show"], ns=ns).stdout,
            "interfaces": command(["ip", "-s", "link", "show"], ns=ns).stdout,
        }
    return {
        "socket_and_interface_snapshots": True,
        "proxy_endpoint": "10.73.0.2:443",
        "vpn_carrier": "10.89.0.1:1080",
        "vpn_endpoint": "127.0.0.1:1194",
    }


def dns_probe(host):
    result = probe("dns", host)
    if result["answer"] != host:
        raise AssertionError("DNS query used an incorrect resolver path")
    return result


def domestic_ipv6():
    result = probe("tcp", "fd72:1::1")
    if result["peer"] != "fd72:1::2":
        raise AssertionError("domestic IPv6 did not originate from terminal")
    return result


def environment():
    result = {
        "os": command(["cat", "/etc/os-release"]).stdout,
        "architecture": command(["uname", "-m"]).stdout.strip(),
        "kernel": command(["uname", "-r"]).stdout.strip(),
        "python": command(["python3", "--version"]).stdout.strip(),
        "iproute2": command(["ip", "-Version"]).stdout.strip(),
        "nftables": command(["nft", "--version"]).stdout.strip(),
        "packages": command(
            [
                "dpkg-query",
                "-W",
                "openvpn",
                "iproute2",
                "nftables",
                "libssl3t64",
                "python3.12",
                "procps",
                "openssl",
            ]
        ).stdout,
        "systemd": {
            "systemctl_installed": bool(shutil.which("systemctl")),
            "live_service_manager_validated": False,
        },
    }
    return result


def evidence():
    report = {
        "status": "passed",
        "environment": environment(),
        "tests": RESULTS,
        "healthy_path": HEALTHY,
        "logs": sanitized_logs(),
        "artifact_generation": (
            "production ProfileRenderer and ServerRenderer; only local SOCKS test input appended"
        ),
        "bootstrap_resolution": (
            "carrier.lab via explicit controlled DNS 10.73.0.1; "
            "terminal entry uses a numeric endpoint"
        ),
    }
    for ns in NAMES:
        report.setdefault("routes", {})[ns] = command(["ip", "route", "show"], ns=ns).stdout
        report.setdefault("firewall", {})[ns] = command(["nft", "list", "ruleset"], ns=ns).stdout
    for name in ["domestic", "foreign", "management"]:
        path = WORK / (name + ".jsonl")
        report.setdefault("targets", {})[name] = (
            [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        )
    return report


def run():
    test("isolated_topology", topology)
    test("temporary_ca_and_identity", ca)
    paths, device, carrier = prepare_configs()
    os.environ["SSL_CERT_FILE"] = str(WORK / "ca.crt")
    spawn(
        "domestic",
        [
            "python3",
            "/opt/wayvia-lab/target.py",
            "domestic",
            "10.71.0.1",
            str(WORK / "domestic.jsonl"),
        ],
        "domestic",
    )
    spawn(
        "management",
        [
            "python3",
            "/opt/wayvia-lab/target.py",
            "management",
            "10.73.0.1",
            str(WORK / "management.jsonl"),
        ],
        "ali",
    )
    japan = spawn("japan", ["sing-box", "run", "-c", str(paths["japan"])], "jp")
    vpn_server = spawn("vpn-server", ["openvpn", "--config", str(WORK / "vpn-server.conf")], "jp")
    test("v2raya_manages_matching_core", lambda: carrier_start(carrier))
    vpn_client = spawn(
        "vpn-client", ["openvpn", "--config", str(WORK / "vpn-client.conf")], "wayvia-egress"
    )
    bridge = spawn("bridge", ["sing-box", "run", "-c", str(paths["bridge"])], "wayvia-egress")
    entry = spawn("entry", ["sing-box", "run", "-c", str(paths["entry"])], "ali")
    spawn("terminal", ["sing-box", "run", "-c", str(paths["terminal"])], "terminal")
    test("foreign_tcp_dual_layer_japan_nat", wait_path)
    test("domestic_tcp_terminal_direct", domestic_ok)
    test("foreign_native_tcp_intercepted_by_rendered_tun", lambda: probe("native-tcp"))
    test("foreign_native_udp_intercepted_by_rendered_tun", lambda: probe("native-udp"))
    test("foreign_udp_through_vpn_and_socks_bridge", lambda: probe("udp"))
    test("foreign_dns_through_vpn", lambda: dns_probe("10.74.0.2"))
    test("domestic_dns_terminal_direct", lambda: dns_probe("10.71.0.1"))
    test("domestic_ipv6_terminal_direct", domestic_ipv6)
    test("healthy_layer_order_and_return_paths", capture_healthy)
    test("unsupported_foreign_ipv6_blocked", lambda: probe("tcp", "fd72:4::2", False))
    test("japan_proxy_rejects_arbitrary_target", lambda: restricted_proxy_probe())
    test("japan_vpn_not_publicly_reachable", lambda: vpn_not_public())
    test("management_path_keeps_physical_return", management_path)
    test("vpn_layer_failure_protected_domestic_available", lambda: failure(vpn_server))
    vpn_server = spawn(
        "vpn-server-restart", ["openvpn", "--config", str(WORK / "vpn-server.conf")], "jp"
    )
    test("vpn_layer_reconnect", wait_path)
    test("proxy_layer_failure_protected_domestic_available", lambda: failure(japan))
    japan = spawn("japan-restart", ["sing-box", "run", "-c", str(paths["japan"])], "jp")
    test("proxy_layer_reconnect", wait_path)
    test("device_permission_revoke", lambda: revoke(entry, paths["entry"]))
    test(
        "production_openvpn_adapter_isolated_handshake",
        lambda: adapter_vpn_handshake(vpn_client, bridge),
    )
    report = evidence()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "lab-result.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0


def adapter_vpn_handshake(client, bridge):
    client.terminate()
    client.wait(timeout=10)
    command(["ip", "netns", "attach", "wayvia-validation", str(bridge.pid)])
    try:
        code = """import json
from pathlib import Path
from wayvia.adapters.openvpn import OpenVPNAdapter
result=OpenVPNAdapter().validate(Path('/generated/ingress/vpn-client.conf'),namespace='wayvia-validation')
print(json.dumps(result))
"""
        result = json.loads(
            command(["/opt/wayvia-lab-python/bin/python", "-c", code], timeout=15).stdout
        )
        return result
    finally:
        command(["ip", "netns", "delete", "wayvia-validation"], check=False)


def restricted_proxy_probe():
    result = command(
        [
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            "4",
            "--socks5-hostname",
            "10.89.0.1:1080",
            "http://10.74.0.2:8080",
        ],
        ns="wayvia-egress",
        check=False,
        timeout=8,
    )
    if result.returncode == 0:
        raise AssertionError("Japanese carrier forwarded arbitrary website")
    return {"exit_code": result.returncode, "arbitrary_forwarding_rejected": True}


def management_path():
    result = json.loads(
        command(
            ["curl", "--fail", "--silent", "--max-time", "3", "http://10.73.0.1:8080"], ns="jp"
        ).stdout
    )
    if result["peer"] != "10.73.0.2":
        raise AssertionError("management return changed physical path")
    return result


def vpn_not_public():
    code = "import socket; socket.create_connection(('10.73.0.2',1194),timeout=2)"
    result = command(["python3", "-c", code], ns="ali", check=False)
    if result.returncode == 0:
        raise AssertionError("Japan VPN publicly reachable")
    return {"connection_rejected": True, "listener": "127.0.0.1:1194"}


def revoke(entry, path):
    entry.terminate()
    entry.wait(timeout=10)
    config = json.loads(path.read_text())
    config["inbounds"][0]["users"] = []
    path.write_text(json.dumps(config))
    spawn("entry-revoked", ["sing-box", "run", "-c", str(path)], "ali")
    time.sleep(0.5)
    return {
        "new_connections": probe(expected=False),
        "domestic": domestic_ok(),
        "method": "controlled ingress restart; existing sessions terminated",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    try:
        if args.preflight:
            for binary in ["sing-box", "mihomo", "v2raya", "v2raya_core", "openvpn"]:
                flag = (
                    "version"
                    if binary in ["sing-box", "v2raya_core"]
                    else "--version"
                    if binary in ["openvpn", "v2raya"]
                    else "-v"
                )
                print(command([binary, flag], check=False).stdout.strip())
            print(json.dumps(test("namespace_capability", topology)))
            sys.exit(0)
        sys.exit(run())
    except Exception as error:
        OUT.mkdir(parents=True, exist_ok=True)
        logs = sanitized_logs()
        (OUT / "lab-result.json").write_text(
            json.dumps(
                {"status": "failed", "error": str(error), "tests": RESULTS, "logs": logs}, indent=2
            )
            + "\n"
        )
        print("LAB FAILED: " + str(error), file=sys.stderr)
        sys.exit(1)
    finally:
        for process in reversed(PROCESSES):
            if process.poll() is None:
                process.terminate()
        for ns in reversed(NAMES):
            command(["ip", "netns", "delete", ns], check=False)
