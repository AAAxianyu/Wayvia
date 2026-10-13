"""Read-only diagnostic layers, with missing input kept visibly unknown."""

import platform
from datetime import UTC, datetime

from wayvia.adapters.base import AdapterError
from wayvia.adapters.openvpn import OpenVPNAdapter
from wayvia.adapters.singbox import SingBoxAdapter
from wayvia.adapters.v2raya import V2rayAAdapter
from wayvia.agent.models import LAYERS

EVIDENCE_MAX_AGE_SECONDS = 90
CLOCK_SKEW_SECONDS = 5


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _fresh(observed_at, valid_until, now):
    observed, expires = _timestamp(observed_at), _timestamp(valid_until)
    return (
        observed is not None
        and expires is not None
        and observed <= now + CLOCK_SKEW_SECONDS
        and observed <= expires <= observed + EVIDENCE_MAX_AGE_SECONDS
        and now <= expires
        and now - observed <= EVIDENCE_MAX_AGE_SECONDS
    )


def transaction_diagnostic(tx, *, now=None):
    """A current generation needs fresh, bound probes and a separate monitor heartbeat."""
    now = datetime.now(UTC).timestamp() if now is None else now
    observation = {
        "role": tx.get("role"),
        "transaction_id": tx.get("id"),
        "generation": tx.get("generation"),
        "revision": tx.get("revision"),
        "state": tx.get("state"),
        "status": "unknown",
    }
    if tx.get("role") not in ("ingress", "exit") or tx.get("current") is not True:
        observation["error"] = "no_current_generation"
        return observation
    if tx.get("simulated") is True:
        observation["error"] = "simulated_evidence"
        return observation
    if tx.get("state") in ("DEGRADED", "RECOVERY_REQUIRED"):
        observation["status"] = "failed"
        observation["error"] = tx.get("blocked_reason") or "deployment_failed"
        return observation
    if tx.get("state") != "ACTIVE":
        observation["error"] = "configuration_unconfirmed"
        return observation
    revision, generation = tx.get("revision"), tx.get("generation")
    expected_scope = "datapath" if tx.get("role") == "ingress" else "component_readiness"
    if tx.get("verification_scope") != expected_scope:
        observation["error"] = "verification_scope_invalid"
        return observation
    observation["verification_scope"] = expected_scope
    if (
        not isinstance(revision, str)
        or not revision
        or not isinstance(generation, str)
        or not generation
        or generation != tx.get("id")
    ):
        observation["error"] = "generation_evidence_missing"
        return observation
    evidence = tx.get("evidence")
    if not (
        isinstance(evidence, list)
        and len(evidence) == len(LAYERS)
        and all(
            isinstance(probe, dict) and isinstance(probe.get("layer"), str) for probe in evidence
        )
        and {probe.get("layer") for probe in evidence} == LAYERS
    ):
        observation["error"] = "path_evidence_incomplete"
        return observation
    for probe in evidence:
        if probe.get("simulated") is True:
            observation["error"] = "simulated_evidence"
            return observation
        if probe.get("scope") != expected_scope:
            observation["error"] = "path_evidence_scope_mismatch"
            return observation
        if probe.get("revision") != revision or probe.get("generation") != generation:
            observation["error"] = "path_evidence_generation_mismatch"
            return observation
        duration = probe.get("duration_ms")
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not 0 <= duration <= EVIDENCE_MAX_AGE_SECONDS * 1000
        ):
            observation["error"] = "path_evidence_duration_invalid"
            return observation
        if not _fresh(probe.get("observed_at"), probe.get("valid_until"), now):
            observation["error"] = "path_evidence_stale"
            return observation
    if any(probe.get("status") == "failed" for probe in evidence):
        observation["status"] = "failed"
        observation["error"] = "path_probe_failed"
        return observation
    if not all(probe.get("status") == "healthy" for probe in evidence):
        observation["error"] = "path_probe_unconfirmed"
        return observation
    monitor_fresh = _fresh(tx.get("monitored_at"), tx.get("monitor_valid_until"), now)
    observation["monitor"] = {
        "status": "observed" if monitor_fresh else "unknown",
        "observed_at": tx.get("monitored_at"),
        "valid_until": tx.get("monitor_valid_until"),
    }
    if not monitor_fresh:
        observation["error"] = "monitor_stale"
        return observation
    observation["status"] = "healthy" if expected_scope == "datapath" else "ready"
    return observation


def dataplane_diagnostic(transactions, *, now=None):
    if not isinstance(transactions, list):
        return {"layer": "dataplane", "status": "unknown", "error": "agent_status_invalid"}
    current = [tx for tx in transactions if isinstance(tx, dict) and tx.get("current") is True]
    if not current:
        return {"layer": "dataplane", "status": "unknown", "error": "no_confirmed_configuration"}
    if any(not isinstance(tx.get("role"), str) for tx in current):
        return {"layer": "dataplane", "status": "unknown", "error": "agent_status_invalid"}
    if len({tx.get("role") for tx in current}) != len(current):
        return {"layer": "dataplane", "status": "unknown", "error": "ambiguous_current_generation"}
    observations = [transaction_diagnostic(tx, now=now) for tx in current]
    status = (
        "failed"
        if any(row["status"] == "failed" for row in observations)
        else "healthy"
        if all(row["status"] == "healthy" for row in observations)
        else "ready"
        if all(row["status"] in {"healthy", "ready"} for row in observations)
        else "unknown"
    )
    return {"layer": "dataplane", "status": status, "roles": observations}


def diagnose(*, runner=None, socket_path="/run/wayvia/agent.sock"):
    layers = [
        {
            "layer": "environment",
            "status": "healthy" if platform.system() == "Linux" else "unsupported",
            "platform": platform.system(),
        }
    ]
    for name, adapter in (
        ("sing-box", SingBoxAdapter(runner)),
        ("openvpn", OpenVPNAdapter(runner)),
        ("v2rayA", V2rayAAdapter()),
    ):
        try:
            observation = adapter.validate() if name == "v2rayA" else adapter.inspect()
            valid = observation.get("supported", observation.get("coreVersionValid"))
            layers.append(
                {"layer": name, "status": "healthy" if valid else "unsupported", **observation}
            )
        except AdapterError as exc:
            layers.append(
                {
                    "layer": name,
                    "status": "unsupported" if exc.code == "VERSION_MISMATCH" else "unknown",
                    "error": exc.code,
                }
            )
    from wayvia.agent.client import AgentClient

    try:
        transactions = AgentClient(socket_path).request({"operation": "status"})
        layers.append({"layer": "agent", "status": "healthy", "transactions": transactions})
        layers.append(dataplane_diagnostic(transactions))
    except AdapterError as exc:
        layers.append({"layer": "agent", "status": "unknown", "error": exc.code})
        layers.append({"layer": "dataplane", "status": "unknown", "error": "agent_unavailable"})
    return {
        "healthy": all(layer["status"] == "healthy" for layer in layers),
        "layers": layers,
        "scope": "software-components; real device/network acceptance "
        "requires independent evidence",
    }
