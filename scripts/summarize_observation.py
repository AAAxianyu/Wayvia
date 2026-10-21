"""Summarize the observer's sanitized JSONL; never reads production plans or logs."""

import argparse
import collections
import json
import re
import statistics
from pathlib import Path


def stats(values):
    if not values:
        return {"count": 0}
    values = sorted(values)
    return {
        "count": len(values),
        "median": statistics.median(values),
        "p95": values[min(len(values) - 1, int(len(values) * 0.95))],
        "max": max(values),
    }


def control_ok(row):
    if "curl_exit" in row:
        return (
            row.get("valid_https_response") is True
            and row.get("single_valid_trace_ip") is True
            and row.get("expected_exit_match", True) is True
        )
    return row.get("exit") == 0 and row.get("rcode") == "NOERROR"


def summarize(directory):
    result = {
        "samples": 0,
        "monitor_states": collections.Counter(),
        "transitions": [],
        "failed_monitor_checks": [],
        "failed_control_examples": [],
        "completed_response_closes": 0,
        "partial_lines_ignored": 0,
        "peer_ping": collections.Counter(),
        "carrier_connection_count": collections.Counter(),
        "controls": {},
    }
    timings = collections.defaultdict(list)
    cpu = []
    ping = []
    previous_cpu = None
    first_tcp = None
    latest_tcp = None
    last_monitor = None
    previous_at = None
    gaps = []
    with (directory / "samples.jsonl").open() as f:
        for line in f:
            # A concurrent writer can leave only the final line incomplete at EOF.
            if not line.endswith("\n"):
                result["partial_lines_ignored"] += 1
                continue
            row = json.loads(line)
            if row["type"] == "start":
                result["start"] = row
                continue
            result["latest_record_at"] = row["at"]
            if row["type"] == "control":
                label, data = row["label"], row["result"]
                counts = result["controls"].setdefault(label, collections.Counter())
                healthy = control_ok(data)
                counts["ok" if healthy else "failed"] += 1
                if not healthy and len(result["failed_control_examples"]) < 100:
                    result["failed_control_examples"].append(row)
                for key in ["time_connect", "time_appconnect", "time_starttransfer", "time_total"]:
                    if healthy and isinstance(data.get(key), (float, int)):
                        timings[label + "_" + key + "_ms"].append(1000 * data[key])
                if healthy and isinstance(data.get("query_ms"), int):
                    timings[label + "_query_ms"].append(data["query_ms"])
                continue
            if row["type"] != "sample":
                continue
            result["samples"] += 1
            if previous_at is not None:
                gaps.append(row["monotonic"] - previous_at)
            previous_at = row["monotonic"]
            current_cpu = row["host"]["cpu_jiffies"]
            if previous_cpu:
                deltas = [a - b for a, b in zip(current_cpu, previous_cpu, strict=True)]
                total = sum(deltas)
                if total > 0 and all(v >= 0 for v in deltas):
                    cpu.append(100 * (total - deltas[3] - deltas[4]) / total)
            previous_cpu = current_cpu
            latest_tcp = row["host"]["host_global_tcp_counters"]
            if first_tcp is None:
                first_tcp = latest_tcp
            peer = row["peer_ping"]
            result["peer_ping"]["ok" if peer["exit"] == 0 else "miss"] += 1
            if peer.get("rtt_ms") is not None:
                ping.append(peer["rtt_ms"])
            connections = row["carrier"].get("known_carrier_connections", [])
            result["carrier_connection_count"][str(len(connections))] += 1
            for connection in connections:
                if "rtt" in connection:
                    timings["carrier_tcp_rtt_ms"].append(float(connection["rtt"].split("/")[0]))
            monitor = row.get("monitor")
            if monitor is None or monitor["updated_at"] == last_monitor:
                continue
            last_monitor = monitor["updated_at"]
            result["monitor_states"][monitor["state"]] += 1
            transitions = result["transitions"]
            if not transitions or transitions[-1]["state"] != monitor["state"]:
                transitions.append({"at": monitor["updated_at"], "state": monitor["state"]})
            for evidence in monitor["evidence"]:
                result["completed_response_closes"] += sum(
                    a["transport_closed_after_response"] for a in evidence["attempts"]
                )
                if evidence["status"] != "healthy":
                    if len(result["failed_monitor_checks"]) < 100:
                        result["failed_monitor_checks"].append(evidence)
                    result["failed_monitor_check_count"] = (
                        result.get("failed_monitor_check_count", 0) + 1
                    )
    result["sample_gap_seconds"] = stats(gaps)
    result["cpu_busy_percent_excluding_iowait"] = stats(cpu)
    result["peer_ping_rtt_ms"] = stats(ping)
    result["timings"] = {key: stats(values) for key, values in timings.items()}
    result["host_global_tcp_delta"] = {
        key: value - first_tcp.get(key, value)
        for key, value in (latest_tcp or {}).items()
        if value >= first_tcp.get(key, value)
    }
    end = directory / "result.json"
    result["end"] = json.loads(end.read_text()) if end.exists() else None
    result["file_bytes"] = (directory / "samples.jsonl").stat().st_size
    result["limits"] = (
        "First 100 failed examples only; counts complete. Sampled monitor snapshots can miss "
        "intermediate states: reconcile with journal. Global TCP counters include other workloads. "
        "ICMP misses alone are not service failures. No phone or failure-injection acceptance."
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    args = parser.parse_args()
    assert re.fullmatch(r"[a-zA-Z0-9-]{1,64}", args.run_id)
    directory = Path("/var/lib/wayvia/observations") / args.run_id
    print(json.dumps(summarize(directory), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
