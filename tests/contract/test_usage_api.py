import json
from types import SimpleNamespace

import pytest
from test_topology import artifacts

from wayvia.adapters.base import AdapterError
from wayvia.adapters.linux import LinuxExecutor
from wayvia.adapters.singbox import usage
from wayvia.agent.models import Action
from wayvia.agent.server import dispatch
from wayvia.agent.validation import validate_server_artifact


def test_owned_api_secret_is_stable_and_confined(tmp_path):
    executor = LinuxExecutor(config_dir=tmp_path / "etc", state_dir=tmp_path / "state")
    action = Action(
        kind="install_config", resource="ingress.json", content=artifacts()["ingress.json"]
    )
    adjusted = executor.prepare_ingress(action)
    assert executor.prepare_ingress(adjusted) == adjusted
    assert (tmp_path / "state/usage-api-secret").stat().st_mode & 0o777 == 0o600
    validate_server_artifact("ingress.json", adjusted.content)
    config = json.loads(adjusted.content)
    for changes in [
        dict(listen="0.0.0.0"),
        dict(listen_port=8080),
        dict(secret=""),
        dict(dashboard=True),
        dict(type="resolved"),
    ]:
        altered = json.loads(adjusted.content)
        altered["services"][0].update(changes)
        with pytest.raises(AdapterError):
            validate_server_artifact("ingress.json", json.dumps(altered))
    with pytest.raises(AdapterError):
        validate_server_artifact("exit.json", json.dumps(config))


def test_agent_does_not_allow_arbitrary_stats_requests():
    manager = SimpleNamespace(executor=SimpleNamespace(device_usage=lambda: {"connections": []}))
    assert dispatch(manager, {"operation": "device_usage"}) == {"connections": []}
    for extra in [
        dict(url="http://example.test"),
        dict(secret="secret"),
        dict(method="CloseConnection"),
    ]:
        with pytest.raises(AdapterError):
            dispatch(manager, {"operation": "device_usage"} | extra)


def test_native_projection_keeps_users_counters_and_discards_probe(monkeypatch):
    connection = usage.MESSAGES["Connection"]
    event = usage.MESSAGES["Event"]
    events = usage.MESSAGES["Events"](
        reset=True,
        events=[
            event(
                connection=connection(
                    id="one", user="a" * 32, createdAt=1000, uplinkTotal=12, downlinkTotal=24
                )
            ),
            event(connection=connection(id="probe", user="wayvia-internal-probe", uplinkTotal=999)),
        ],
    )
    cancelled = []

    class Stream:
        def __next__(self):
            return events

        def cancel(self):
            cancelled.append(True)

    class Channel:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def unary_unary(self, path, **kwargs):
            assert path in [
                "/daemon.StartedService/GetVersion",
                "/daemon.StartedService/GetStartedAt",
            ]

            def call(request, **kw):
                assert kw["metadata"] == (("authorization", "Bearer secret"),)
                assert kw["timeout"] <= 3
                if path.endswith("GetVersion"):
                    return usage.MESSAGES["Version"](version="1.14.3", apiVersion=4)
                return usage.MESSAGES["StartedAt"](startedAt=1000)

            return call

        def unary_stream(self, path, **kwargs):
            assert path == "/daemon.StartedService/SubscribeConnections"
            return lambda *a, **kw: Stream()

    monkeypatch.setattr(usage.grpc, "insecure_channel", lambda *a, **k: Channel())
    result = usage.snapshot("secret")
    assert cancelled == [True]
    assert len(result["connections"]) == 1
    assert result["connections"][0]["upload"] == 12
    assert "secret" not in json.dumps(result) and "probe" not in json.dumps(result)
