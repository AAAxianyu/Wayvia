"""Layered observations are evidence, never empty-success defaults."""

from wayvia.adapters.base import AdapterError


def verify(executor, probes):
    evidence = []
    for probe in probes:
        try:
            if probe.kind == "unsupported":
                raise AdapterError("PROBE_UNSUPPORTED", "Probe inputs are unavailable")
            if probe.kind == "service":
                result = executor._cmd(
                    ["systemctl", "is-active", probe.target], timeout=probe.timeout
                )
                healthy = result.stdout.strip() == "active"
            elif probe.kind == "tcp":
                result = executor._cmd(
                    ["nc", "-z", "-w", str(probe.timeout), probe.target, str(probe.port)],
                    namespace=probe.namespace,
                    timeout=probe.timeout + 1,
                )
                healthy = result.returncode == 0
            elif probe.kind == "route":
                result = executor._cmd(
                    ["ip", "-j", "route", "get", probe.target],
                    namespace=probe.namespace,
                    timeout=probe.timeout,
                )
                import json

                healthy = any(row.get("dev") == probe.expected for row in json.loads(result.stdout))
            elif probe.kind == "http":
                result = executor._cmd(
                    [
                        "curl",
                        "--silent",
                        "--show-error",
                        "--fail",
                        "--max-time",
                        str(probe.timeout),
                        "--",
                        probe.target,
                    ],
                    namespace=probe.namespace,
                    timeout=probe.timeout + 1,
                )
                healthy = result.stdout.strip() == probe.expected
            elif probe.kind == "dns":
                result = executor._cmd(
                    [
                        "dig",
                        "+short",
                        "+tries=1",
                        f"+time={probe.timeout}",
                        "@" + probe.target,
                        "-p",
                        str(probe.port),
                        probe.expected,
                        "A",
                    ],
                    namespace=probe.namespace,
                    timeout=probe.timeout + 1,
                )
                healthy = bool(result.stdout.strip())
            elif probe.kind == "ipv6_guard":
                if probe.namespace:
                    result = executor._cmd(
                        ["nft", "list", "table", "inet", "wayvia_guard"],
                        namespace="wayvia-egress",
                        timeout=probe.timeout,
                    )
                    healthy = (
                        "policy drop" in result.stdout
                        and 'oifname "tun0" meta nfproto ipv4 accept' in result.stdout
                    )
                else:
                    import json

                    config = json.loads((executor.config_dir / "exit.json").read_text())
                    vpn = (executor.config_dir / "vpn-server.conf").read_text()
                    healthy = (
                        config["route"]["rules"][-1] == {"action": "reject"}
                        and config["route"]["rules"][0]["ip_cidr"] == ["127.0.0.1/32"]
                        and "server-ipv6" not in vpn
                    )
            else:
                raise AdapterError("PROBE_UNSUPPORTED", "Probe kind has no implementation")
            evidence.append(
                {
                    "layer": probe.layer,
                    "status": "healthy" if healthy else "failed",
                    "kind": probe.kind,
                }
            )
        except AdapterError as exc:
            evidence.append(
                {"layer": probe.layer, "status": "failed", "kind": probe.kind, "error": exc.code}
            )
    return evidence
