"""Layered observations are evidence, never empty-success defaults."""

import json
import sys
import time
from datetime import UTC, datetime

from wayvia.adapters.base import AdapterError


def verify(executor, probes, *, deadline_monotonic=None):
    evidence = []
    for probe in probes:
        started = time.monotonic()
        observed_at = datetime.now(UTC).isoformat()
        timeout = (
            min(probe.timeout, max(0, deadline_monotonic - started))
            if deadline_monotonic is not None
            else probe.timeout
        )
        detail = {}
        try:
            if timeout <= 0:
                raise AdapterError("PROBE_DEADLINE", "Transaction verification deadline expired")
            authenticated_kinds = {
                "access": {"trojan_http", "trojan_vpn"},
                "carrier": {"vpn_handshake"},
                "vpn": {"vpn_session"},
            }
            if (
                probe.layer in authenticated_kinds
                and probe.kind not in authenticated_kinds[probe.layer]
            ):
                raise AdapterError(
                    "PROBE_UPGRADE_REQUIRED",
                    "Replan legacy reachability probes with authenticated protocol checks",
                )
            if probe.kind in {"http", "trojan_http", "trojan_vpn", "vpn_handshake", "vpn_session"}:
                payload = probe.model_dump()
                payload.update(
                    config_dir=str(executor.config_dir), timeout=max(0.01, timeout - 0.5)
                )
                result = executor._cmd(
                    [sys.executable, "-m", "wayvia.agent.probe_runtime"],
                    namespace=probe.namespace,
                    input_text=json.dumps(payload),
                    timeout=timeout,
                    process_tree=True,
                )
                observation = json.loads(result.stdout)
                if not isinstance(observation, dict):
                    raise AdapterError("PROBE_INVALID", "Invalid protocol observation")
                healthy = observation.get("healthy") is True
                detail = {
                    key: observation[key]
                    for key in ("check", "client_authenticated", "peer_count", "error", "attempts")
                    if key in observation
                }
            elif probe.kind == "unsupported":
                raise AdapterError("PROBE_UNSUPPORTED", "Probe inputs are unavailable")
            elif probe.kind == "service":
                result = executor._cmd(["systemctl", "is-active", probe.target], timeout=timeout)
                healthy = result.stdout.strip() == "active"
            elif probe.kind == "tcp":
                result = executor._cmd(
                    ["nc", "-z", "-w", str(timeout), probe.target, str(probe.port)],
                    namespace=probe.namespace,
                    timeout=timeout,
                )
                healthy = result.returncode == 0
            elif probe.kind == "route":
                result = executor._cmd(
                    ["ip", "-j", "route", "get", probe.target],
                    namespace=probe.namespace,
                    timeout=timeout,
                )
                healthy = any(row.get("dev") == probe.expected for row in json.loads(result.stdout))
            elif probe.kind == "dns":
                result = executor._cmd(
                    [
                        "dig",
                        "+short",
                        "+tries=1",
                        f"+time={max(1, int(timeout))}",
                        "@" + probe.target,
                        "-p",
                        str(probe.port),
                        probe.expected,
                        "A",
                    ],
                    namespace=probe.namespace,
                    timeout=timeout,
                )
                healthy = bool(result.stdout.strip())
            elif probe.kind == "ipv6_guard":
                if probe.namespace:
                    result = executor._cmd(
                        ["nft", "list", "table", "inet", "wayvia_guard"],
                        namespace="wayvia-egress",
                        timeout=timeout,
                    )
                    healthy = (
                        "policy drop" in result.stdout
                        and 'oifname "tun0" meta nfproto ipv4 accept' in result.stdout
                    )
                else:
                    config = json.loads((executor.config_dir / "exit.json").read_text())
                    vpn = (executor.config_dir / "vpn-server.conf").read_text()
                    healthy = (
                        config["route"]["rules"][-1] == {"action": "reject"}
                        and config["route"]["rules"][0]["ip_cidr"] == ["127.0.0.1/32"]
                        and "server-ipv6" not in vpn
                    )
            else:
                raise AdapterError("PROBE_UNSUPPORTED", "Probe kind has no implementation")
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                raise AdapterError("PROBE_DEADLINE", "Transaction verification deadline expired")
            evidence.append(
                {
                    "layer": probe.layer,
                    "status": "healthy" if healthy else "failed",
                    "kind": probe.kind,
                    **detail,
                }
            )
        except (AdapterError, ValueError, KeyError, OSError) as exc:
            evidence.append(
                {
                    "layer": probe.layer,
                    "status": "failed",
                    "kind": probe.kind,
                    "error": exc.code if isinstance(exc, AdapterError) else "PROBE_INVALID",
                }
            )
        evidence[-1].update(
            observed_at=observed_at,
            duration_ms=round((time.monotonic() - started) * 1000),
            scope="datapath"
            if any(p.namespace == "wayvia-egress" for p in probes)
            else "component_readiness",
        )
    return evidence
