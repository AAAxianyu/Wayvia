"""Read-only diagnostic layers, with missing input kept visibly unknown."""

import platform

from wayvia.adapters.base import AdapterError
from wayvia.adapters.openvpn import OpenVPNAdapter
from wayvia.adapters.singbox import SingBoxAdapter
from wayvia.adapters.v2raya import V2rayAAdapter
from wayvia.agent.models import LAYERS


def dataplane_diagnostic(transactions):
    if not isinstance(transactions, list):
        return {"layer": "dataplane", "status": "unknown", "error": "agent_status_invalid"}
    relevant = [
        tx
        for tx in transactions
        if isinstance(tx, dict) and tx.get("state") not in {"ROLLED_BACK", "SUPERSEDED"}
    ]
    if not relevant:
        return {"layer": "dataplane", "status": "unknown", "error": "no_confirmed_configuration"}
    current = {}
    for tx in relevant:
        role = tx.get("role", "unknown")
        if (
            role not in current
            or (current[role].get("state") == "ACTIVE" and tx.get("state") != "ACTIVE")
            or (
                current[role].get("state") == tx.get("state")
                and tx.get("updated_at", "") > current[role].get("updated_at", "")
            )
        ):
            current[role] = tx
    observations = []
    for role, tx in current.items():
        state = tx.get("state")
        if role not in {"ingress", "exit"}:
            status = "unknown"
        elif state in {"DEGRADED", "RECOVERY_REQUIRED"}:
            status = "failed"
        elif state != "ACTIVE":
            status = "unknown"
        else:
            evidence = tx.get("evidence")
            complete = (
                isinstance(evidence, list)
                and len(evidence) == len(LAYERS)
                and all(isinstance(probe, dict) for probe in evidence)
                and {probe.get("layer") for probe in evidence} == LAYERS
            )
            if not complete:
                status = "unknown"
            elif any(probe.get("status") == "failed" for probe in evidence):
                status = "failed"
            else:
                status = (
                    "healthy"
                    if all(probe.get("status") == "healthy" for probe in evidence)
                    else "unknown"
                )
        observations.append(
            {"role": role, "transaction_id": tx.get("id"), "state": state, "status": status}
        )
    status = (
        "failed"
        if any(row["status"] == "failed" for row in observations)
        else "healthy"
        if all(row["status"] == "healthy" for row in observations)
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
