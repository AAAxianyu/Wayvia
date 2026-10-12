from pathlib import Path

import pytest
from test_topology import artifacts, probes

from wayvia.adapters.base import AdapterError
from wayvia.adapters.linux import LinuxExecutor, unit_content
from wayvia.agent.planner import build_plan
from wayvia.agent.transactions import TransactionManager


class ExpiredRunner:
    def run(self, argv, **kwargs):
        assert argv[:3] == ["openssl", "x509", "-checkend"]
        raise AdapterError("COMMAND_FAILED", "expired")


class Timer:
    simulated = False

    def __init__(self):
        self.events = []

    def arm(self, *args):
        self.events.append("armed")

    def cancel(self, *args):
        self.events.append("cancelled")


def test_expired_certificate_rejected_before_timer_or_network(tmp_path, monkeypatch):
    executor = LinuxExecutor(
        runner=ExpiredRunner(),
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        unit_dir=tmp_path / "units",
        enforce_environment=False,
    )
    monkeypatch.setattr(executor, "baseline", lambda actions: "baseline")
    monkeypatch.setattr("wayvia.agent.validation.secret_file", lambda *args, **kwargs: None)
    timer = Timer()
    manager = TransactionManager(executor, tmp_path / "state", timer)
    plan = build_plan("ingress", "1", artifacts(), "baseline", probes=probes())
    with pytest.raises(AdapterError) as error:
        manager.apply(plan)
    assert error.value.code == "CERTIFICATE_INVALID"
    assert timer.events == []
    assert manager.status() == []
    assert not (tmp_path / "config").exists()


def test_recovery_and_data_units_have_no_mutual_ordering_deadlock():
    recover = Path("deploy/systemd/wayvia-recover.service").read_text()
    assert "Before=wayvia-agent.service\n" in recover
    assert "wayvia-ingress.service" not in recover
    assert "wayvia-exit.service" not in recover
    for unit in ("wayvia-ingress.service", "wayvia-exit.service", "wayvia-bridge.service"):
        text = unit_content(unit)
        assert "wayvia-recover.service" not in text
