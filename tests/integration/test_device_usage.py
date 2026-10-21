import json

import pytest
from fastapi.testclient import TestClient

from wayvia.api import create_app
from wayvia.config import WayviaConfig, example_config
from wayvia.services.control import ControlService, SimulatedDataplane
from wayvia.services.usage import UsageStore


@pytest.fixture
def control(tmp_path):
    return ControlService(
        tmp_path,
        WayviaConfig.model_validate(example_config()),
        mode="test",
        dataplane=SimulatedDataplane(),
    )


def sample(now=100, connections=(), generation="one", **kwargs):
    return (
        dict(
            observed_at=now,
            connections=list(connections),
            generation=generation,
            closed_buffer_full=False,
            oldest_closed_at=None,
        )
        | kwargs
    )


def conn(device, ident="c1", upload=100, download=200, closed=None):
    return dict(
        id=ident,
        device_id=device,
        created_at=95,
        closed_at=closed,
        upload=upload,
        download=download,
    )


def test_independent_activity_and_durable_delta_accounting(control):
    phone = control.add("phone", "android")["id"]
    desktop = control.add("computer", "desktop")["id"]
    store = UsageStore(control.db)
    store.record(sample(connections=[conn(phone)]))
    assert store.device(phone, now=101)["online"] is True
    assert store.device(desktop, now=101)["online"] is False
    assert store.device(desktop, now=101)["last_used_at"] is None
    store.record(sample(105, [conn(phone, upload=140, download=300)]))
    store.record(sample(104, [conn(phone, upload=400, download=600)]))  # late sample ignored
    recovered = UsageStore(control.db)
    recovered.record(sample(110, [conn(phone, upload=140, download=300, closed=108)]))
    recovered.record(sample(115, [conn(phone, upload=140, download=300, closed=108)]))
    usage = recovered.device(phone, now=116)
    assert usage["online"] is False
    assert usage["total_bytes"] == 440
    assert usage["last_used_at"] == 105  # idle open/close is not fresh traffic
    assert recovered.device(desktop, now=116)["total_bytes"] == 0
    assert recovered.device(phone, now=140)["status"] == "unknown"
    assert recovered.device(phone, now=140)["online"] is None


def test_short_closed_connections_and_generation_changes(control):
    device = control.add("phone", "android")["id"]
    store = UsageStore(control.db)
    store.record(sample(connections=[conn(device, closed=99)]))
    assert store.device(device, now=101)["last_used_at"] == 99
    store.record(sample(105, [conn(device, ident="c2", upload=20, download=30)], generation="two"))
    usage = store.device(device, now=106)
    assert usage["total_bytes"] == 350 and usage["gaps"] == 1
    assert usage["last_gap_reason"] == "core_generation_changed"


def test_missing_final_counter_and_closed_ring_overflow_are_explicit(control):
    device = control.add("phone", "android")["id"]
    store = UsageStore(control.db)
    store.record(sample(connections=[conn(device)]))
    store.record(sample(105, [], closed_buffer_full=True, oldest_closed_at=102))
    assert store.device(device, now=106)["gaps"] == 2
    assert store.device(device, now=106)["total_bytes"] == 300


def test_fetch_is_not_proxy_use_and_metadata_is_private(control):
    device = control.add("desktop", "desktop")["id"]
    subscription = control.issue(device, "mihomo")
    control.fetch(subscription["token"], "mihomo")
    usage = control.device(device)["usage"]
    assert usage["last_subscription_fetch_at"] is not None
    assert usage["subscription_fetches"] == 1
    assert usage["last_used_at"] is None and usage["online"] is None
    client = TestClient(create_app(control, "a" * 48))
    assert client.get("/api/v1/subscriptions").status_code == 401
    metadata = client.get("/api/v1/subscriptions", headers={"Authorization": "Bearer " + "a" * 48})
    assert metadata.status_code == 200
    assert subscription["token"] not in metadata.text and "token_hash" not in metadata.text
    public = TestClient(create_app(control, "a" * 48, subscriptions_only=True))
    assert public.get("/api/v1/subscriptions").status_code == 404
    assert public.get("/api/v1/devices").status_code == 404


def test_failure_and_simulation_never_report_offline(control):
    device = control.add("phone", "android")["id"]
    store = UsageStore(control.db)
    store.record(sample(connections=[conn(device)]))
    store.failure("USAGE_UNAVAILABLE")
    assert store.device(device, now=101)["online"] is None
    assert store.device(device, now=101)["total_bytes"] == 300
    assert control.collect_usage()["status"] == "unavailable"
    assert "password" not in json.dumps(control.devices())
