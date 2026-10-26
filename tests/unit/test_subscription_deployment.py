"""A finished observer may be absent after boot; required units must still be readable."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "subscription_deployment", Path(__file__).parents[2] / "deploy/update_subscription_api.py"
)
deployment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deployment)


def test_absent_finished_observer_is_readonly_evidence(monkeypatch):
    calls = []

    def missing(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "LoadState=not-found\n", "")

    monkeypatch.setattr(deployment.subprocess, "run", missing)
    assert (
        deployment.unit_state("wayvia-observe-stability-20261020.service")
        == "LoadState=not-found\n"
    )
    assert len(calls) == 1 and calls[0][:2] == ["systemctl", "show"]
    with pytest.raises(RuntimeError, match="Required service"):
        deployment.unit_state("wayvia-ingress.service")


def test_existing_service_snapshot_preserves_the_real_state(monkeypatch):
    state = "LoadState=loaded\nMainPID=123\nActiveState=active\nSubState=running\n"
    monkeypatch.setattr(
        deployment.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, state, ""),
    )
    assert deployment.unit_state("wayvia-ingress.service") == state
