"""Finite read-only TCP queue observation; no traffic generation or raw socket output."""

import datetime
import hashlib
import json
import re
import subprocess
import sys
import time


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def snapshot(host):
    peer = "206.119.182.131" if host == "ali" else "8.136.0.78"
    result = subprocess.run(
        ["ss", "-Htinmp"], capture_output=True, text=True, timeout=3
    )
    selected = []
    current = None
    for line in result.stdout.splitlines():
        if line and not line[0].isspace():
            fields = line.split()
            current = None
            if len(fields) < 5:
                continue
            local, remote = fields[3:5]
            carrier = remote.rsplit(":", 1)[0] == peer and (
                remote.endswith(":443") if host == "ali" else local.endswith(":443")
            )
            vpn = '"openvpn"' in line
            if not carrier and not vpn:
                continue
            current = {
                "id": hashlib.sha256((local + " " + remote).encode()).hexdigest()[:16],
                "layer": "carrier" if carrier else "vpn_transport",
                "state": fields[0],
                "recv_queue_bytes": int(fields[1]),
                "send_queue_bytes": int(fields[2]),
                "peer_is_loopback": remote.startswith(("127.", "[::1]:")),
            }
            selected.append(current)
        elif current is not None:
            for key in (
                "rtt", "rto", "cwnd", "retrans", "bytes_sent", "bytes_acked",
                "bytes_received", "bytes_retrans", "lastsnd", "lastrcv", "unacked",
                "notsent", "minrtt", "rcv_rtt", "rcv_space", "rcv_ssthresh", "snd_wnd",
                "busy", "rwnd_limited", "sndbuf_limited", "segs_out", "segs_in",
                "data_segs_out", "data_segs_in",
            ):
                match = re.search(r"\b" + key + r":([0-9.]+(?:/[0-9.]+)?)(ms)?", line)
                if match:
                    current[key] = match[1]
            for key in ("send", "pacing_rate", "delivery_rate"):
                match = re.search(r"\b" + key + r" ([0-9.]+)([KMG]?)bps", line)
                if match:
                    current[key + "_bps"] = float(match[1]) * {
                        "": 1, "K": 1000, "M": 1000000, "G": 1000000000
                    }[match[2]]
            match = re.search(r"skmem:\(([0-9a-z,]+)\)", line)
            if match:
                current["socket_memory_bytes"] = {
                    k: int(v) for k, v in re.findall(r"([a-z]+)([0-9]+)", match[1])
                }
    return {"exit": result.returncode, "connections": selected}


def main():
    host, count_text = sys.argv[1:]
    assert host in {"ali", "jp"}
    count = int(count_text)
    assert 1 <= count <= 120
    print(json.dumps({"type": "start", "at": now(), "host": host,
                      "cycles": count, "interval_seconds": 5}), flush=True)
    started = time.monotonic()
    for i in range(count):
        time.sleep(max(0, started + i * 5 - time.monotonic()))
        print(json.dumps({"type": "sample", "at": now(), "index": i,
                          **snapshot(host)}), flush=True)
    print(json.dumps({"type": "end", "at": now(), "reason": "cycles_complete",
                      "samples": count}), flush=True)


if __name__ == "__main__":
    main()
