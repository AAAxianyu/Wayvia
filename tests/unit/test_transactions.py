import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from wayvia.adapters.base import AdapterError
from wayvia.agent.models import Action, State
from wayvia.agent.transactions import TransactionManager


class Timer:
    simulated = True

    def __init__(self, fail=False):
        self.fail, self.events = fail, []

    def arm(self, transaction_id, seconds, journal_dir):
        if self.fail:
            raise AdapterError("TIMER_FAILED", "cannot arm")
        self.events.append(("armed", transaction_id))

    def cancel(self, transaction_id):
        self.events.append(("cancelled", transaction_id))


class Executor:
    simulated = True

    def __init__(self, fail_at=None, rollback_fail=False):
        self.events = []
        self.fail_at, self.rollback_fail = fail_at, rollback_fail

    def baseline(self, actions):
        return "baseline"

    def preflight(self, plan):
        return None

    def capture(self, action):
        return {"exists": False, "owned": False}

    def apply(self, action):
        self.events.append(("apply", action.resource))
        if len([e for e in self.events if e[0] == "apply"]) == self.fail_at:
            raise AdapterError("INJECTED", "failure")

    def rollback(self, action, before):
        self.events.append(("rollback", action.resource))
        if self.rollback_fail:
            raise AdapterError("RESTORE_FAILED", "failure")

    def restore_services(self, steps):
        self.events.append(("restore_services", len(steps)))

    def protect(self, plan):
        self.events.append(("protect", plan.role))

    def validate_devices(self, devices):
        return None

    def sync_devices(self, devices, revision):
        self.events.append(("devices", revision))
        return {"status": "applied", "revision": revision}

    def _cmd(self, argv):
        self.events.append(("command", argv))


def plan():
    from wayvia.agent.planner import build_plan

    return build_plan(
        "ingress",
        "1",
        {
            name: "{}"
            for name in (
                "ingress.json",
                "bridge.json",
                "carrier.json",
                "vpn-client.conf",
                "manifest.json",
            )
        },
        "baseline",
    )


def healthy(executor, probes):
    return [{"layer": p.layer, "status": "healthy"} for p in probes]


def manager(tmp_path, executor=None, timer=None, verifier=healthy):
    return TransactionManager(
        executor or Executor(), tmp_path, timer or Timer(), mode="test", verifier=verifier
    )


def test_explicit_confirmation_and_independent_timeout(tmp_path):
    m = manager(tmp_path)
    tx = m.apply(plan())
    assert tx["state"] == State.AWAITING_CONFIRM
    assert m._read(tx["id"])["steps"]
    assert (tmp_path / (tx["id"] + ".json")).stat().st_mode & 0o077 == 0
    restarted = manager(tmp_path, m.executor)
    assert restarted.recover()[0]["state"] == State.ROLLED_BACK
    with pytest.raises(AdapterError, match="Only a successfully verified"):
        restarted.confirm(tx["id"])


@pytest.mark.parametrize("point", range(1, 14))
def test_every_partial_action_failure_recovers(tmp_path, point):
    e = Executor(fail_at=point)
    tx = manager(tmp_path, e).apply(plan())
    assert tx["state"] == State.ROLLED_BACK
    applied = [r for event, r in e.events if event == "apply"]
    restored = [r for event, r in e.events if event == "rollback"]
    assert restored == list(reversed(applied))


def test_timer_failure_prevents_all_network_mutation(tmp_path):
    e = Executor()
    tx = manager(tmp_path, e, Timer(fail=True)).apply(plan())
    assert tx["state"] == State.ROLLED_BACK
    assert not any(event == "apply" for event, _ in e.events)


def test_failed_probe_and_failed_rollback_are_never_active(tmp_path):
    e = Executor(rollback_fail=True)
    m = manager(tmp_path, e, verifier=lambda e, p: [{"status": "unknown"}])
    assert m.apply(plan())["state"] == State.RECOVERY_REQUIRED
    assert ("protect", "ingress") in e.events


def test_expired_confirmation_rolls_back(tmp_path):
    m = manager(tmp_path)
    tx = m.apply(plan())
    saved = m._read(tx["id"])
    saved["deadline"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    m._save(saved)
    with pytest.raises(AdapterError) as err:
        m.confirm(tx["id"])
    assert err.value.code == "CONFIRM_EXPIRED"
    assert m.status()[0]["state"] == State.ROLLED_BACK


def test_new_confirmation_supersedes_previous_and_fault_blocks(tmp_path):
    m = manager(tmp_path)
    first = m.confirm(m.apply(plan())["id"])
    second = m.confirm(m.apply(plan())["id"])
    assert m._read(first["id"])["state"] == State.SUPERSEDED
    assert m._read(second["id"])["state"] == State.ACTIVE
    with pytest.raises(AdapterError) as err:
        m.rollback(first["id"])
    assert err.value.code == "TRANSACTION_SUPERSEDED"
    m.verifier = lambda e, p: [{"status": "failed"}]
    assert m.monitor(second["id"])["state"] == State.DEGRADED
    assert ("protect", "ingress") in m.executor.events


def test_active_reboot_establishes_guard_and_verifies(tmp_path):
    m = manager(tmp_path)
    tx = m.confirm(m.apply(plan())["id"])
    m.executor.events.clear()
    assert m.recover(rebuild_active=True)[0]["state"] == State.ACTIVE
    assert m.executor.events[0] == ("protect", "ingress")
    assert m._read(tx["id"])["state"] == State.ACTIVE


def test_latest_credentials_override_network_recovery_and_reject_replay(tmp_path):
    m = manager(tmp_path)
    m.sync_devices([{"id": "new", "password": "n" * 32}], 3)
    action = Action(
        kind="install_config",
        resource="ingress.json",
        content=json.dumps(
            {"inbounds": [{"type": "trojan", "users": [{"name": "revoked", "password": "old"}]}]}
        ),
    )
    recovered = json.loads(m._credential_action(action).content)
    assert recovered["inbounds"][0]["users"] == [{"name": "new", "password": "n" * 32}]
    import base64

    before = {"exists": True, "content": base64.b64encode(action.content.encode()).decode()}
    adjusted = m._credential_snapshot(action, before)
    assert "revoked" not in base64.b64decode(adjusted["content"]).decode()
    with pytest.raises(AdapterError) as err:
        m.sync_devices([], 2)
    assert err.value.code == "REVISION_STALE"
    # Nontransaction metadata cannot be mistaken for a recovery journal.
    assert m.status() == []


def test_plan_digest_expiry_baseline_and_fixed_actions():
    p = plan()
    for candidate, baseline, code in (
        (p.model_copy(update={"revision": "tampered"}), "baseline", "PLAN_TAMPERED"),
        (p, "changed", "BASELINE_CHANGED"),
        (p, "baseline", "PLAN_EXPIRED"),
    ):
        with pytest.raises(AdapterError) as err:
            candidate.check(baseline, now=p.expires_at if code == "PLAN_EXPIRED" else None)
        assert err.value.code == code
    with pytest.raises(ValidationError):
        Action(kind="service", resource="sshd.service")
    with pytest.raises(ValidationError):
        Action(kind="guard", resource="wayvia_guard", content="flush ruleset")


def test_production_refuses_fake(tmp_path):
    with pytest.raises(AdapterError) as err:
        TransactionManager(Executor(), tmp_path, Timer())
    assert err.value.code == "FAKE_FORBIDDEN"


def test_explicit_block_is_durable_across_restart(tmp_path):
    m = manager(tmp_path)
    tx = m.confirm(m.apply(plan())["id"])
    assert m.block_ingress() == {"status": "blocked"}
    assert m._read(tx["id"])["state"] == State.DEGRADED
    m.executor.events.clear()
    assert m.recover(rebuild_active=True) == []
    assert not any(event == "apply" for event, _ in m.executor.events)


def test_crash_between_new_active_and_superseding_old_replays_only_latest(tmp_path):
    m = manager(tmp_path)
    first = m.confirm(m.apply(plan())["id"])
    second = m.confirm(m.apply(plan())["id"])
    old = m._read(first["id"])
    old["state"] = State.ACTIVE
    m._save(old)  # Later updated_at must not outrank the new confirmed_at.
    m.executor.events.clear()
    result = m.recover(rebuild_active=True)
    assert len(result) == 1
    assert result[0]["id"] == second["id"]
    assert m._read(first["id"])["state"] == State.SUPERSEDED
