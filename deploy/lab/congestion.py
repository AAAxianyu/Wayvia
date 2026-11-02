#!/usr/bin/env python3
"""Shared-egress congestion experiment for ADR 014, inside the disposable path lab.

The Japan namespace gets one uplink (`jfa`, the production `ens17` analogue) into a
provider bridge namespace. The provider limits everything Japan sends with a FIFO
token bucket, like a cloud instance's outbound cap. Device downloads (Japan to the
Ali carrier) and uploads (Japan to the foreign target) therefore share one cap, and
Wayvia's optional shaping sits on Japan's own NIC in front of it.
"""

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, "/opt/wayvia-lab")
import run  # noqa: E402  The real dual-layer lab: rendered configs and real components.

PYTHON = "/opt/wayvia-lab-python/bin/python"
TARGET = "10.74.0.2"
PROBE_TIMEOUT = 5.75  # Matches the production HTTP probe attempt budget.
SHAPING = {"egress_kbit": 1800, "carrier_kbit": 1200, "carrier_port": 443}
LOAD_SERVER = """
import json, socket, sys, threading
received = [0]
def sink(conn):
    with conn:
        while True:
            data = conn.recv(65536)
            if not data:
                break
            received[0] += len(data)
def status(conn):
    with conn:
        conn.sendall(json.dumps({"received": received[0]}).encode())
def source(conn):
    block = b"\\0" * 65536
    with conn:
        try:
            while True:
                conn.sendall(block)
        except OSError:
            pass
def serve(port, handler):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((sys.argv[1], port))
    listener.listen(64)
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=handler, args=(conn,), daemon=True).start()
threading.Thread(target=serve, args=(9000, sink), daemon=True).start()
threading.Thread(target=serve, args=(9002, status), daemon=True).start()
serve(9001, source)
"""
LOAD_CLIENT = """
import json, socket, struct, sys, threading, time
host, port, seconds, streams = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
moved = [0] * streams
errors = []
deadline = time.monotonic() + seconds
def worker(index):
    block = b"\\1" * 16384
    try:
        conn = socket.create_connection((host, port), timeout=10)
        conn.settimeout(2)
        while time.monotonic() < deadline:
            try:
                if port == 9000:
                    moved[index] += conn.send(block)
                else:
                    data = conn.recv(65536)
                    if not data:
                        break
                    moved[index] += len(data)
            except socket.timeout:
                continue
        # Reset instead of closing: the local TUN stack has accepted far more than
        # the path carried, and a graceful close would keep uploading it afterwards.
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()
    except OSError as error:
        errors.append(f"{index}:{round(deadline - time.monotonic(), 1)}s_left:{error!r}"[:200])
threads = [threading.Thread(target=worker, args=(i,)) for i in range(streams)]
start = time.monotonic()
[t.start() for t in threads]
[t.join(seconds + 15) for t in threads]
print(json.dumps({"bytes": sum(moved), "seconds": time.monotonic() - start,
                  "per_stream": moved, "errors": errors}))
"""


def link(first, second, label, address1, address2, original=run.link):
    """Route Japan's two links through one provider bridge; others are unchanged."""
    if label == "aj":
        # topology() already created the namespace listed in run.NAMES.
        run.command(
            ["ip", "link", "add", "br0", "type", "bridge", "forward_delay", "0", "stp_state", "0"],
            ns="provider",
        )
        run.command(["ip", "link", "set", "br0", "up"], ns="provider")
        for host_ns, host_if, bridge_if, address in (
            ("ali", "aja", "ajb", "10.73.0.1/24"),
            ("jp", "jfa", "jwb", "10.73.0.2/24"),
        ):
            run.command(["ip", "link", "add", host_if, "type", "veth", "peer", "name", bridge_if])
            run.command(["ip", "link", "set", host_if, "netns", host_ns])
            run.command(["ip", "link", "set", bridge_if, "netns", "provider"])
            run.command(["ip", "addr", "add", address, "dev", host_if], ns=host_ns)
            run.command(["ip", "link", "set", host_if, "up"], ns=host_ns)
            run.command(["ip", "link", "set", bridge_if, "master", "br0", "up"], ns="provider")
    elif label == "jf":
        run.command(["ip", "link", "add", "jfb", "type", "veth", "peer", "name", "fwb"])
        run.command(["ip", "link", "set", "jfb", "netns", "foreign"])
        run.command(["ip", "link", "set", "fwb", "netns", "provider"])
        run.command(["ip", "addr", "add", "10.74.0.2/24", "dev", "jfb"], ns="foreign")
        run.command(["ip", "link", "set", "jfb", "up"], ns="foreign")
        run.command(["ip", "link", "set", "fwb", "master", "br0", "up"], ns="provider")
        run.command(["ip", "addr", "add", "10.74.0.1/24", "dev", "jfa"], ns="jp")
        # Warm the bridge and neighbours before the lab's one-second baseline check.
        for ns, peer in (("ali", "10.73.0.2"), ("jp", "10.73.0.1"), ("jp", "10.74.0.2")):
            for _attempt in range(50):
                if run.command(["ping", "-c1", "-W1", peer], ns=ns, check=False).returncode == 0:
                    break
            else:
                raise RuntimeError("provider bridge did not forward to " + peer)
    else:
        original(first, second, label, address1, address2)


def provider_limit(rate, limit):
    """Everything Japan transmits passes one FIFO token bucket before the bridge."""
    run.command(["ip", "link", "add", "ifb0", "type", "ifb"], ns="provider")
    run.command(["ip", "link", "set", "ifb0", "up"], ns="provider")
    run.command(["tc", "qdisc", "add", "dev", "jwb", "handle", "ffff:", "ingress"], ns="provider")
    run.command(
        ["tc", "filter", "add", "dev", "jwb", "parent", "ffff:", "matchall"]
        + ["action", "mirred", "egress", "redirect", "dev", "ifb0"],
        ns="provider",
    )
    run.command(
        ["tc", "qdisc", "add", "dev", "ifb0", "root", "tbf", "rate", rate]
        + ["burst", "16kb", "limit", limit],
        ns="provider",
    )
    # Japan forwards ali->foreign on the same NIC; redirects would only add noise.
    run.command(["sysctl", "-w", "net.ipv4.conf.all.send_redirects=0"], ns="jp")
    run.command(["sysctl", "-w", "net.ipv4.conf.jfa.send_redirects=0"], ns="jp")
    return {"rate": rate, "fifo_limit": limit, "scope": "all_japan_transmit"}


def timed_curl(ns, extra):
    argv = ["curl", "--silent", "--output", "/dev/null", "--max-time", str(PROBE_TIMEOUT)]
    argv += ["--write-out", "%{time_connect} %{time_starttransfer} %{http_code}"]
    started = time.monotonic()
    result = run.command(argv + extra, ns=ns, check=False, timeout=PROBE_TIMEOUT + 5)
    elapsed = time.monotonic() - started
    fields = result.stdout.split()
    ok = result.returncode == 0 and fields[-1:] == ["200"]
    return {"ok": ok, "seconds": round(elapsed, 3), "curl": result.returncode}


def probes(stop, rows):
    """Production-like fresh requests: the egress layer and a full device path."""
    while not stop.is_set():
        rows.append(
            {
                "at": time.monotonic(),
                # Egress layer: wayvia-egress -> OpenVPN tun -> Japan -> target.
                "egress": timed_curl("wayvia-egress", [f"http://{TARGET}:8080"]),
                # Device path: terminal SOCKS -> entry -> carrier -> VPN -> Japan.
                "device": timed_curl(
                    "terminal",
                    ["--socks5-hostname", "127.0.0.1:1080", f"http://{TARGET}:8080"],
                ),
            }
        )
        stop.wait(1.0)


def carrier_dirs():
    """Production counter code on both roles; the lab carrier uses a DNS name."""
    exit_dir = Path("/generated/exit")
    ingress = run.WORK / "ingress-cfg"
    shutil.copytree("/generated/ingress", ingress, dirs_exist_ok=True)
    config = json.loads((ingress / "carrier.json").read_text())
    for outbound in config["outbounds"]:
        if outbound.get("tag") == "japan-carrier":
            outbound["server"] = "10.73.0.2"
    (ingress / "carrier.json").write_text(json.dumps(config))
    return {"ingress": (ingress, "ali"), "exit": (exit_dir, "jp")}


COUNTER = """import json,sys
from wayvia.adapters.linux import LinuxExecutor
print(json.dumps(LinuxExecutor(config_dir=sys.argv[1],enforce_environment=False).carrier_counters()))
"""


def counters(dirs):
    result = {}
    for role, (directory, ns) in dirs.items():
        output = run.command([PYTHON, "-c", COUNTER, str(directory)], ns=ns, check=False)
        result[role] = json.loads(output.stdout) if output.returncode == 0 else None
    return result


def progress(stop, dirs, rows):
    """The ADR 014 signal: does the same carrier socket keep receiving bytes?"""
    previous = counters(dirs)
    while not stop.wait(15):
        current = counters(dirs)
        row = {}
        for role in dirs:
            before, after = previous.get(role), current.get(role)
            row[role] = (
                None
                if not before or not after or before["socket"] != after["socket"]
                else after["received"] > before["received"]
            )
        rows.append(row)
        previous = current


SINK = """import json,socket
print(socket.create_connection(("10.74.0.2", 9002), timeout=5).recv(4096).decode())
"""


def sink_bytes():
    """Upload goodput is what reached the target, not what a local TUN accepted."""
    return json.loads(run.command(["python3", "-c", SINK], ns="foreign").stdout)["received"]


def queues():
    """Where Japan holds data: socket send queues and the qdiscs on its NIC and tun."""
    sockets = []
    output = run.command(["ss", "-tinmpH", "state", "established"], ns="jp").stdout
    for line in output.splitlines():
        if line and not line[0].isspace():
            fields = line.split()
            sockets.append(
                {
                    "recv_q": int(fields[0]),
                    "send_q": int(fields[1]),
                    "local": fields[2],
                    "peer": fields[3],
                    "process": (line.split('(("')[1].split('"')[0] if '(("' in line else None),
                }
            )
        elif sockets:
            for key in ("notsent", "unacked", "cwnd"):
                match = __import__("re").search(rf"\b{key}:(\d+)", line)
                if match:
                    sockets[-1][key] = int(match.group(1))
    qdiscs = {}
    for device in ("jfa", "tun0"):
        text = run.command(
            ["tc", "-s", "qdisc", "show", "dev", device], ns="jp", check=False
        ).stdout
        qdiscs[device] = [line.strip() for line in text.splitlines() if "backlog" in line]
    provider = run.command(["tc", "-s", "qdisc", "show", "dev", "ifb0"], ns="provider").stdout
    return {
        "jp_sockets": sorted(sockets, key=lambda row: -row["send_q"])[:6],
        "jp_qdisc_backlog": qdiscs,
        "provider_backlog": [line.strip() for line in provider.splitlines() if "backlog" in line],
    }


def summarize(rows, key):
    values = [row[key]["seconds"] for row in rows]
    ok = [row[key]["ok"] for row in rows]
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))] if values else None
    return {
        "samples": len(values),
        "failed": ok.count(False),
        "over_3s": sum(v > 3 for v in values),
        "p50_s": round(statistics.median(values), 3) if values else None,
        "p95_s": p95,
        "max_s": max(values) if values else None,
    }


def quiesce(timeout=180):
    """Start every scenario idle: the sink stops growing and Japan's carrier drains.

    Each proxy hop closes gracefully, so tens of MB a finished upload left in relay
    buffers would keep draining at the capped rate; reset the load sockets instead.
    """
    run.command(
        ["ss", "-K", "state", "established", "(", "sport", "=", ":9000", "or"]
        + ["sport", "=", ":9001", ")"],
        ns="foreign",
        check=False,
    )
    started, last, stable = time.monotonic(), sink_bytes(), 0
    while time.monotonic() - started < timeout:
        time.sleep(1)
        now = sink_bytes()
        carrier = [s for s in queues()["jp_sockets"] if s["local"].endswith(":443")]
        idle = now == last and all(s["send_q"] < 16384 for s in carrier)
        stable = stable + 1 if idle else 0
        last = now
        if stable >= 5:
            return round(time.monotonic() - started, 1)
    raise RuntimeError("path did not return to idle between scenarios")


def scenario(name, direction, seconds, streams, dirs):
    stop, rows, marks = threading.Event(), [], []
    workers = [
        threading.Thread(target=probes, args=(stop, rows)),
        threading.Thread(target=progress, args=(stop, dirs, marks)),
    ]
    loads = {}
    for kind in {
        "upload": ["upload"],
        "download": ["download"],
        "both": ["upload", "download"],
    }.get(direction, []):
        loads[kind] = subprocess.Popen(
            ["ip", "netns", "exec", "terminal", "python3", "-c", LOAD_CLIENT]
            + [TARGET, str({"upload": 9000, "download": 9001}[kind]), str(seconds), str(streams)],
            stdout=subprocess.PIPE,
            text=True,
        )
    if loads:
        time.sleep(3)  # Let the transfer fill the queues before measuring.
    received = sink_bytes()
    started = time.monotonic()
    for worker in workers:
        worker.start()
    time.sleep(min(20, seconds / 2))
    snapshot = queues()
    goodput = {}
    for kind, load in loads.items():
        moved = json.loads(load.communicate(timeout=seconds + 60)[0])
        goodput[kind + "_raw"] = {
            key: moved.get(key) for key in ("bytes", "seconds", "per_stream", "errors")
        }
        if kind == "upload":
            # Sender-side counts include what the local TUN stack buffered.
            moved = {"bytes": sink_bytes() - received, "seconds": time.monotonic() - started}
        goodput[kind] = round(moved["bytes"] * 8 / moved["seconds"] / 1e6, 3)
        if moved.get("errors"):
            goodput[kind + "_errors"] = moved["errors"]
    if not loads:
        time.sleep(max(0, seconds - (time.monotonic() - started)))
    stop.set()
    for worker in workers:
        worker.join(30)
    result = {
        "scenario": name,
        "load": direction or "idle",
        "streams": streams if direction else 0,
        "seconds": seconds,
        "goodput_mbit": goodput or None,
        "egress_probe": summarize(rows, "egress"),
        "device_probe": summarize(rows, "device"),
        "queues_mid_load": snapshot,
        "carrier_progress_15s": {
            role: [row[role] for row in marks] for role in ("ingress", "exit")
        },
    }
    print(json.dumps(result), flush=True)
    return result


def shaping(enabled, lowat, tunnel=None, carrier=None):
    spec = (
        {**SHAPING, "notsent_lowat": lowat, "tunnel_kbit": tunnel}
        | ({"carrier_kbit": carrier} if carrier else {})
        if enabled
        else None
    )
    code = f"""import json
from wayvia.adapters.linux import LinuxExecutor
from wayvia.agent.models import Action, ShapingSpec
spec = json.loads({json.dumps(json.dumps(spec))})
host = LinuxExecutor(config_dir='/tmp/jp-cfg', state_dir='/tmp/jp-shape',
                     unit_dir='/tmp/jp-units', enforce_environment=False)
action = Action.model_construct(kind='shaping', resource='wayvia_shaping', interface='jfa',
                namespace=None, content=None, operation='ensure', subnet=None,
                # model_construct only so the lab can show a rejected mismatch.
                shaping=ShapingSpec.model_construct(**spec) if spec else None)
host.apply(action)
print(json.dumps(host.inspect_action(action)))
"""
    state = json.loads(run.command([PYTHON, "-c", code], ns="jp").stdout)
    kinds = {}
    for device in ("jfa", "tun0"):
        qdisc = run.command(["tc", "qdisc", "show", "dev", device], ns="jp", check=False).stdout
        kinds[device] = sorted(
            {line.split()[1] for line in qdisc.splitlines() if line.startswith("qdisc")}
        )
    return {"state": state, "qdisc_kinds": kinds}


LAB = {}
VPNBUF = "sndbuf 65536\nrcvbuf 65536\ntcp-queue-limit 16\n"


def vpn_options(extra):
    """Restart the Japan OpenVPN server with extra buffer options (lab knob)."""
    if LAB.get("vpn_extra", "") == extra:
        return
    LAB["vpn_server"].terminate()
    LAB["vpn_server"].wait(timeout=10)
    (run.WORK / "vpn-server.conf").write_text(LAB["vpn_base"] + extra)
    LAB["vpn_server"] = run.spawn(
        "vpn-server-" + str(len(run.PROCESSES)),
        ["openvpn", "--config", str(run.WORK / "vpn-server.conf")],
        "jp",
    )
    LAB["vpn_extra"] = extra
    run.wait_path()


ENSURE = """import json
from wayvia.adapters.linux import LinuxExecutor
host = LinuxExecutor(config_dir='/tmp/jp-cfg', state_dir='/tmp/jp-shape',
                     unit_dir='/tmp/jp-units', enforce_environment=False)
print(json.dumps(host.ensure_shaping()))
"""


def tun_root():
    rows = json.loads(run.command(["tc", "-j", "qdisc", "show", "dev", "tun0"], ns="jp").stdout)
    return next(q["kind"] for q in rows if q.get("root"))


def tunnel_restore():
    """Production self-heal: OpenVPN restarts recreate tun0 without the fair queue."""
    shaping(True, 131072, 1450, 1600)
    applied = tun_root()
    LAB["vpn_extra"] = None  # Force a real restart with the unchanged configuration.
    vpn_options("")
    after_restart = tun_root()
    restored = json.loads(run.command([PYTHON, "-c", ENSURE], ns="jp").stdout)
    again = json.loads(run.command([PYTHON, "-c", ENSURE], ns="jp").stdout)
    result = {
        "applied_root": applied,
        "root_after_openvpn_restart": after_restart,
        "ensure_restored": restored,
        "root_after_ensure": tun_root(),
        "second_ensure_noop": again is False,
    }
    if (
        applied != "htb"
        or after_restart == "htb"
        or not restored
        or result["root_after_ensure"] != "htb"
        or again
    ):
        raise AssertionError("tunnel fair queue was not restored: " + json.dumps(result))
    return result


def grace_safety(vpn_server, japan, paths):
    """Ingress stays up (the grace period) while a protected layer dies under load."""
    load = subprocess.Popen(
        ["ip", "netns", "exec", "terminal", "python3", "-c", LOAD_CLIENT]
        + [TARGET, "9000", "40", "4"],
        stdout=subprocess.PIPE,
        text=True,
    )
    time.sleep(5)
    results = {}
    seen = run.WORK / "foreign.jsonl"
    before = len(seen.read_text().splitlines()) if seen.exists() else 0
    results["vpn_layer_dead"] = run.failure(vpn_server)
    vpn_server = run.spawn(
        "vpn-server-grace", ["openvpn", "--config", str(run.WORK / "vpn-server.conf")], "jp"
    )
    results["vpn_reconnect"] = run.wait_path()
    results["proxy_layer_dead"] = run.failure(japan)
    japan = run.spawn("japan-grace", ["sing-box", "run", "-c", str(paths["japan"])], "jp")
    results["proxy_reconnect"] = run.wait_path()
    load.communicate(timeout=120)
    # Every request that reached the foreign target came from the Japan NAT address.
    peers = {
        json.loads(line).get("peer")
        for line in seen.read_text().splitlines()[before:]
        if line.strip()
    }
    if peers - {"10.74.0.1"}:
        raise AssertionError("foreign target saw a non-Japan source: " + str(peers))
    results["foreign_sources_during_test"] = sorted(peers)
    return results


# name, load, tcp_notsent_lowat (None = no shaping), OpenVPN server extra, tun0 kbit.
# name, load, tcp_notsent_lowat (None = no Wayvia shaping), OpenVPN server extra,
# tun0 kbit, applied through the production ShapingSpec.tunnel_kbit.
VARIANTS = (
    ("idle_baseline", None, None, "", None),
    ("upload_unshaped", "upload", None, "", None),
    ("download_unshaped", "download", None, "", None),
    ("both_unshaped", "both", None, "", None),
    ("upload_nic", "upload", 131072, "", None),
    ("download_nic", "download", 131072, "", None),
    ("both_nic", "both", 131072, "", None),
    ("upload_tuned", "upload", 131072, "", 1450, 1600),
    ("download_tuned", "download", 131072, "", 1450, 1600),
    ("both_tuned", "both", 131072, "", 1450, 1600),
    ("idle_tuned", None, 131072, "", 1450, 1600),
    # Lab-only mismatch (rejected by ShapingSpec): tun above the carrier guarantee.
    ("both_mismatch_1600_over_1200", "both", 131072, "", 1600, None),
)
EXPLORATION = (
    ("download_lowat16k", "download", 16384, "", None),
    ("download_lowat16k_vpnbuf", "download", 16384, VPNBUF, None),
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--streams", type=int, default=4)
    parser.add_argument("--provider-limit", default="256kb")
    parser.add_argument("--only", nargs="*", help="variant names; default runs all")
    args = parser.parse_args()
    run.link = link
    run.NAMES.append("provider")
    # HTTPServer resolves its own address at bind time; the container resolver is
    # unreachable here, so answer locally instead of waiting for a DNS timeout.
    with open("/etc/hosts", "a") as hosts:
        hosts.write("10.74.0.2 foreign.lab\n10.71.0.1 domestic.lab\n10.73.0.1 management.lab\n")
    report = {"started": time.time(), "tests": run.RESULTS}
    run.OUT.mkdir(parents=True, exist_ok=True)
    run.test("isolated_topology", run.topology)
    report["provider"] = run.test(
        "provider_shared_fifo", lambda: provider_limit("2mbit", args.provider_limit)
    )
    run.test("temporary_ca_and_identity", run.ca)
    paths, _device, carrier = run.prepare_configs()
    os.environ["SSL_CERT_FILE"] = str(run.WORK / "ca.crt")
    run.spawn(
        "domestic",
        [
            "python3",
            "/opt/wayvia-lab/target.py",
            "domestic",
            "10.71.0.1",
            str(run.WORK / "d.jsonl"),
        ],
        "domestic",
    )
    run.spawn(
        "management",
        [
            "python3",
            "/opt/wayvia-lab/target.py",
            "management",
            "10.73.0.1",
            str(run.WORK / "m.jsonl"),
        ],
        "ali",
    )
    loads = run.WORK / "load_server.py"
    loads.write_text(LOAD_SERVER)
    run.spawn("load-server", ["python3", str(loads), TARGET], "foreign")
    japan = run.spawn("japan", ["sing-box", "run", "-c", str(paths["japan"])], "jp")
    vpn_server = run.spawn(
        "vpn-server", ["openvpn", "--config", str(run.WORK / "vpn-server.conf")], "jp"
    )
    run.test("v2raya_manages_matching_core", lambda: run.carrier_start(carrier))
    run.spawn(
        "vpn-client",
        ["openvpn", "--config", str(run.WORK / "vpn-client.conf")],
        "wayvia-egress",
    )
    run.spawn("bridge", ["sing-box", "run", "-c", str(paths["bridge"])], "wayvia-egress")
    run.spawn("entry", ["sing-box", "run", "-c", str(paths["entry"])], "ali")
    terminal = run.spawn("terminal", ["sing-box", "run", "-c", str(paths["terminal"])], "terminal")
    run.test("foreign_tcp_dual_layer_japan_nat", run.wait_path)
    run.test("terminal_tun_kernel_ready", lambda: run.wait_terminal_tun(terminal))
    dirs = carrier_dirs()
    matrix = []
    LAB.update(vpn_server=vpn_server, vpn_base=(run.WORK / "vpn-server.conf").read_text())
    selected = [v for v in VARIANTS + EXPLORATION if v[0] in args.only] if args.only else VARIANTS
    for name, direction, lowat, vpn_extra, tun_kbit, *carrier in selected:
        vpn_options(vpn_extra)
        state = shaping(lowat is not None, lowat, tun_kbit, carrier[0] if carrier else None)
        tun = tun_kbit
        settled = quiesce()
        result = scenario(name, direction, args.seconds, args.streams, dirs)
        result["settled_before_s"] = settled
        result.update(shaping=state, notsent_lowat=lowat, vpn_server_extra=vpn_extra, tun_kbit=tun)
        matrix.append(result)
        # Keep finished scenarios even if a later step fails.
        (run.OUT / "congestion-partial.json").write_text(json.dumps(matrix, indent=2) + "\n")
        time.sleep(5)
    report["matrix"] = matrix
    (run.OUT / "congestion-partial.json").unlink(missing_ok=True)
    vpn_options("")
    report["tunnel_restore"] = run.test(
        "tunnel_fair_queue_restored_after_openvpn_restart", tunnel_restore
    )
    report["grace_safety"] = run.test(
        "grace_period_layer_failure_no_physical_fallback",
        lambda: grace_safety(LAB["vpn_server"], japan, paths),
    )
    report["shaping_removed"] = shaping(False, None)
    report["environment"] = {
        "kernel": os.uname().release,
        "provider_limit": args.provider_limit,
        "probe_timeout_s": PROBE_TIMEOUT,
        "streams": args.streams,
        "seconds_per_scenario": args.seconds,
    }
    report["status"] = "passed"
    run.OUT.mkdir(parents=True, exist_ok=True)
    name = f"congestion-result-{args.provider_limit}.json"
    (run.OUT / name).write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        run.OUT.mkdir(parents=True, exist_ok=True)
        (run.OUT / "congestion-failed.json").write_text(
            json.dumps({"status": "failed", "error": str(error), "tests": run.RESULTS}, indent=2)
        )
        print("CONGESTION LAB FAILED: " + str(error), file=sys.stderr)
        sys.exit(1)
    finally:
        for process in reversed(run.PROCESSES):
            if process.poll() is None:
                process.terminate()
        for ns in reversed(run.NAMES):
            run.command(["ip", "netns", "delete", ns], check=False)
