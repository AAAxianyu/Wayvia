#!/usr/bin/env python3
"""Delay real kernel TUN policy stages and exercise the lab's exact readiness gate."""

import concurrent.futures
import importlib.util
import json
import os
import queue
import sys
from pathlib import Path


def main():
    if not (
        sys.platform == "linux"
        and Path("/.dockerenv").exists()
        and os.environ.get("WAYVIA_LAB_DISPOSABLE") == "1"
    ):
        raise RuntimeError("Requires an explicitly disposable Linux Docker container")
    spec = importlib.util.spec_from_file_location("wayvia_lab", "/opt/wayvia-lab/run.py")
    lab = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lab)
    observations = queue.Queue()
    stage = "interface_down"
    snapshot = lab.terminal_tun_snapshot

    def observed():
        value = {**snapshot(), "stage": stage}
        observations.put(value)
        return value

    lab.terminal_tun_snapshot = observed

    def run(args):
        return lab.command(args, ns="terminal")

    evidence = []
    reader = None
    try:
        lab.command(["ip", "netns", "add", "terminal"])
        run(["ip", "link", "set", "lo", "up"])
        run(["ip", "link", "add", "physical", "type", "dummy"])
        run(["ip", "addr", "add", "10.72.0.2/24", "dev", "physical"])
        run(["ip", "link", "set", "physical", "up"])
        run(["ip", "route", "add", "default", "via", "10.72.0.1", "dev", "physical"])
        run(["ip", "tuntap", "add", "dev", "wayvia-tun", "mode", "tun"])
        run(["ip", "addr", "add", "172.28.0.1/30", "dev", "wayvia-tun"])
        # Keep the real TUN descriptor open: LOWER_UP must accompany IFF_UP.
        reader = lab.spawn(
            "tun-reader",
            [
                "python3",
                "-c",
                "import os,fcntl,struct,signal; fd=os.open('/dev/net/tun',os.O_RDWR); "
                "fcntl.ioctl(fd,0x400454ca,struct.pack('16sH',b'wayvia-tun',0x1001)); "
                "signal.pause()",
            ],
            "terminal",
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(lab.wait_terminal_tun, reader, 10)

            def rejected(expected):
                while True:
                    state = observations.get(timeout=5)
                    if state["stage"] == expected:
                        break
                assert state["ready"] is False, state
                assert not waiting.done(), "Gate released before source and policy were ready"
                evidence.append(state)

            rejected(stage)
            run(["ip", "link", "set", "wayvia-tun", "up"])
            run(["ip", "route", "add", "default", "dev", "wayvia-tun", "table", "2022"])
            stage = "interface_up_without_policy"
            rejected(stage)
            run(["ip", "rule", "add", "priority", "9003", "from", "0.0.0.0/32", "table", "2022"])
            stage = "initial_lookup_only_without_source_rule"
            rejected(stage)
            run(
                [
                    "ip",
                    "rule",
                    "add",
                    "priority",
                    "9003",
                    "from",
                    "172.28.0.0/30",
                    "table",
                    "2022",
                ]
            )
            stage = "complete_policy"
            result = waiting.result(timeout=5)
            assert result["ready"] is True
            assert result["selected_source"] == "172.28.0.1"
            evidence.append(result)

        run(["ip", "rule", "del", "priority", "9003", "from", "172.28.0.0/30", "table", "2022"])
        try:
            lab.wait_terminal_tun(reader, timeout=0.1)
        except RuntimeError as error:
            assert "readiness timeout" in str(error)
            timeout_rejected = True
        else:
            raise AssertionError("Incomplete policy was accepted after the deadline")
        output = {
            "status": "passed",
            "scope": "isolated Linux kernel readiness; no sing-box runtime modification",
            "deliberately_withheld_policy_stages": evidence,
            "incomplete_policy_timeout_rejected": timeout_rejected,
        }
        Path("/evidence").mkdir(exist_ok=True)
        Path("/evidence/tun-ready-result.json").write_text(json.dumps(output, indent=2) + "\n")
        print(json.dumps(output))
    finally:
        if reader is not None and reader.poll() is None:
            reader.terminate()
            reader.wait(timeout=5)
        lab.command(["ip", "netns", "delete", "terminal"], check=False)


if __name__ == "__main__":
    main()
