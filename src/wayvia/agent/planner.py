"""Compile role artifacts to fixed actions; no shell or caller-provided nft code."""

import json
from datetime import UTC, datetime, timedelta

from wayvia.agent.models import LAYERS, Action, Plan, Probe


def build_plan(
    role,
    revision,
    artifacts,
    baseline,
    *,
    probes=(),
    confirm_seconds=120,
    valid_seconds=300,
    exit_interface=None,
):
    actions = [
        Action(kind="install_config", resource=name, content=content)
        for name, content in sorted(artifacts.items())
    ]
    if role == "ingress":
        actions += [
            Action(kind="namespace", resource="wayvia-egress"),
            Action(kind="guard", resource="wayvia_guard", namespace="wayvia-egress"),
            Action(kind="veth", resource="wayvia-host"),
            Action(kind="guard", resource="wayvia_guard"),
            Action(kind="service", resource="wayvia-carrier.service", operation="restart"),
            Action(kind="service", resource="wayvia-vpn-client.service", operation="restart"),
            Action(kind="service", resource="wayvia-bridge.service", operation="restart"),
            Action(kind="service", resource="wayvia-ingress.service", operation="restart"),
        ]
    elif role == "exit":
        subnet = json.loads(artifacts["manifest.json"])["vpn_subnet"]
        actions += [
            Action(
                kind="exit_nat", resource="wayvia_exit", interface=exit_interface, subnet=subnet
            ),
            Action(kind="service", resource="wayvia-vpn-server.service", operation="restart"),
            Action(kind="service", resource="wayvia-exit.service", operation="restart"),
        ]
    else:
        raise ValueError("role must be ingress or exit")
    provided = {p.layer for p in probes}
    probes = tuple(probes) + tuple(
        Probe(layer=layer, kind="unsupported") for layer in sorted(LAYERS - provided)
    )
    now = datetime.now(UTC)
    return Plan(
        role=role,
        revision=str(revision),
        baseline=baseline,
        created_at=now,
        expires_at=now + timedelta(seconds=valid_seconds),
        actions=tuple(actions),
        probes=probes,
        confirm_seconds=confirm_seconds,
    ).seal()
