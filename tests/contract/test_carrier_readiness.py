"""Startup delay must not turn an otherwise valid rollback into recovery failure."""

import json
import time

import httpx
import pytest

from wayvia.adapters.base import AdapterError, CommandResult
from wayvia.adapters.linux import LinuxExecutor
from wayvia.adapters.v2raya import V2rayAAdapter
from wayvia.agent.models import Action


@pytest.fixture
def clock(monkeypatch):
    current = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: current[0])
    monkeypatch.setattr(time, "sleep", lambda delay: current.__setitem__(0, current[0] + delay))
    return current


def version_response(**overrides):
    return httpx.Response(
        200,
        json={
            "code": "SUCCESS",
            "data": {
                "version": "2.5.10",
                "coreVersion": "2.5.10",
                "coreVersionValid": True,
                **overrides,
            },
        },
    )


class Runner:
    def __init__(self):
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        return CommandResult(0, "")


@pytest.mark.parametrize("operation", ["apply", "rollback"])
def test_carrier_restart_waits_for_delayed_api_before_configuring(
    tmp_path, monkeypatch, clock, operation
):
    requests, configured = [], []

    def handler(request):
        requests.append((request.url.path, clock[0]))
        if clock[0] < 3:
            raise httpx.ConnectError("not listening yet", request=request)
        if request.url.path == "/api/version":
            return version_response()
        assert request.url.path == "/api/account"
        return httpx.Response(200, json={"code": "SUCCESS", "data": {"hasAnyAccounts": True}})

    adapter = V2rayAAdapter(
        client=httpx.Client(
            base_url="http://127.0.0.1:2017", transport=httpx.MockTransport(handler)
        )
    )
    monkeypatch.setattr("wayvia.adapters.v2raya.V2rayAAdapter", lambda **kwargs: adapter)
    monkeypatch.setattr(
        adapter, "configure_carrier", lambda *args, **kwargs: configured.append(clock[0])
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "carrier.json").write_text(json.dumps({}))
    runner = Runner()
    executor = LinuxExecutor(
        runner=runner,
        config_dir=config_dir,
        state_dir=tmp_path / "state",
        unit_dir=tmp_path / "units",
        enforce_environment=False,
    )
    action = Action(kind="service", resource="wayvia-carrier.service")
    if operation == "apply":
        executor.apply(action)
    else:
        executor.restore_services([{"action": action, "before": {"active": True}}])
    assert configured and configured[0] >= 3
    assert all(observed >= 3 for path, observed in requests if path == "/api/account")
    assert ["systemctl", "restart", "wayvia-carrier.service"] in runner.calls


def test_permanently_unavailable_carrier_has_bounded_deadline(clock):
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ConnectError("not listening", request=request)

    adapter = V2rayAAdapter(
        client=httpx.Client(
            base_url="http://127.0.0.1:2017", transport=httpx.MockTransport(handler)
        )
    )
    with pytest.raises(AdapterError) as error:
        adapter.wait_ready(timeout=3)
    assert error.value.code == "V2RAYA_UNAVAILABLE"
    assert clock[0] == pytest.approx(3)
    assert all(request.url.path == "/api/version" for request in requests)


def test_slow_read_consumes_remaining_startup_budget(clock):
    budgets = []

    def handler(request):
        budget = request.extensions["timeout"]["read"]
        budgets.append(budget)
        clock[0] += budget
        raise httpx.ReadTimeout("delayed", request=request)

    adapter = V2rayAAdapter(
        client=httpx.Client(
            base_url="http://127.0.0.1:2017", transport=httpx.MockTransport(handler)
        )
    )
    with pytest.raises(AdapterError) as error:
        adapter.wait_ready(timeout=3)
    assert error.value.code == "V2RAYA_UNAVAILABLE"
    assert clock[0] == pytest.approx(3)
    assert budgets[0] <= 2 and budgets[-1] < budgets[0]


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(200, text="not-json secret"), "V2RAYA_INVALID_RESPONSE"),
        (
            httpx.Response(200, json={"code": "FAIL", "errorCode": "DENIED"}),
            "V2RAYA_REQUEST_FAILED",
        ),
        (version_response(version="2.5.9"), "VERSION_MISMATCH"),
        (version_response(coreVersionValid=False), "VERSION_MISMATCH"),
    ],
)
def test_terminal_readiness_errors_are_not_retried(clock, response, code):
    requests = []

    def handler(request):
        requests.append(request)
        return response

    adapter = V2rayAAdapter(
        client=httpx.Client(
            base_url="http://127.0.0.1:2017", transport=httpx.MockTransport(handler)
        )
    )
    with pytest.raises(AdapterError) as error:
        adapter.wait_ready(timeout=3)
    assert error.value.code == code
    assert len(requests) == 1 and clock[0] == 0
    assert "secret" not in json.dumps(error.value.as_dict())


def test_readiness_response_after_deadline_is_not_accepted(clock):
    def handler(request):
        clock[0] += 3
        return version_response()

    adapter = V2rayAAdapter(
        client=httpx.Client(
            base_url="http://127.0.0.1:2017", transport=httpx.MockTransport(handler)
        )
    )
    with pytest.raises(AdapterError) as error:
        adapter.wait_ready(timeout=3)
    assert error.value.code == "V2RAYA_UNAVAILABLE"
