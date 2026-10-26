"""Software-only lifecycle checks: no services, network, reboot or fault injection."""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from wayvia import observation_run as lifecycle
from wayvia.observation_run import ObservationError, ObservationRun, request_stop


@pytest.fixture
def setup(tmp_path, monkeypatch):
    now = [datetime(2026, 10, 26, tzinfo=UTC)]
    monotonic = [1000.0]
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: monotonic[0])
    monkeypatch.setattr(lifecycle, "boot_id", lambda: "a" * 36)
    contract = {
        "role": "ingress",
        "revision": "b" * 64,
        "generation": "c" * 32,
        "until": (now[0] + timedelta(seconds=60)).isoformat(),
        "max_bytes": 16384,
        "sample_interval_seconds": 5,
        "inputs_digest": "d" * 64,
    }
    return tmp_path / "observations", contract, now, monotonic


def open_run(setup, **changes):
    root, contract, now, _ = setup
    return ObservationRun(
        root, "sample-run", {**contract, **changes}, now=lambda: now[0], resume=True
    )


def test_interruption_requires_explicit_resume_mode(setup):
    root, contract, now, _ = setup
    with open_run(setup) as run:
        run.finish("interrupted")
    with pytest.raises(ObservationError, match="RESUME_REQUIRED"):
        with ObservationRun(root, "sample-run", contract, now=lambda: now[0]):
            pass


def test_resume_preserves_old_bytes_partial_tail_and_original_budget(setup):
    root, _, now, mono = setup
    with open_run(setup) as run:
        run.writer.put({"type": "sample", "at": now[0].isoformat()})
        run.finish("interrupted")
    path = root / "sample-run" / "samples.jsonl"
    with path.open("ab") as file:
        file.write(b'{"partial":')
    before = path.read_bytes()
    now[0] += timedelta(seconds=10)
    mono[0] += 10
    with open_run(setup) as run:
        assert run.resumed and run.remaining() == 50
        assert run.partial_tails == 1 and run.last_at is not None
        assert run.writer.used > len(before)
        run.finish("interrupted")
    assert path.read_bytes() == before
    assert (root / "sample-run" / "samples-000002.jsonl").exists()
    assert (root / "sample-run" / "result-000001.json").exists()
    assert (root / "sample-run" / "result-000002.json").exists()


def test_duplicate_process_is_rejected_and_lock_released(setup):
    with open_run(setup):
        with pytest.raises(ObservationError, match="ALREADY_RUNNING"):
            with open_run(setup):
                pytest.fail("second writer")
    # A lost process without a final checkpoint can resume under the same contract.
    with open_run(setup) as run:
        assert run.resumed


def test_explicit_stop_persists_and_cannot_be_resumed(setup):
    root, _, _, _ = setup
    with open_run(setup) as run:
        request_stop(root, "sample-run")
        assert run.remaining() == 0
        assert run.finish("interrupted") == "user_stop"
    with pytest.raises(ObservationError, match="STOPPED"):
        with open_run(setup):
            pass


@pytest.mark.parametrize(
    "reason", ["deadline", "size_limit", "collector_error", "generation_changed"]
)
def test_terminal_result_survives_stale_state_checkpoint(setup, reason):
    root, _, _, _ = setup
    with open_run(setup) as run:
        run.finish(reason)
        # Simulate stale mutable state; immutable result must still prevent restart.
        run._state("running")
    with pytest.raises(ObservationError, match="TERMINAL"):
        with open_run(setup):
            pass
    assert len(list((root / "sample-run").glob("samples*.jsonl"))) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"generation": "a" * 32},
        {"revision": "e" * 64},
        {"inputs_digest": "f" * 64},
        {"max_bytes": 99999},
        {"until": "2026-10-26T00:02:00+00:00"},
    ],
)
def test_resume_cannot_change_authorization_contract(setup, changes):
    with open_run(setup) as run:
        run.finish("interrupted")
    with pytest.raises(ObservationError, match="CONTRACT_CHANGED"):
        with open_run(setup, **changes):
            pass


def test_clock_changes_never_extend_live_or_resumed_run(setup):
    _, _, now, mono = setup
    with open_run(setup) as run:
        mono[0] += 20
        now[0] -= timedelta(seconds=20)
        assert run.remaining() == 40
        run.finish("interrupted")
    with pytest.raises(ObservationError, match="CLOCK_REGRESSION"):
        with open_run(setup):
            pass
    now[0] += timedelta(seconds=20)
    with open_run(setup) as run:
        assert run.remaining() == 40
    now[0] += timedelta(seconds=100)
    with pytest.raises(ObservationError, match="DEADLINE"):
        with open_run(setup):
            pass


def test_expired_and_legacy_evidence_never_reopen(setup):
    root, _, now, mono = setup
    with open_run(setup):
        pass
    now[0] += timedelta(seconds=61)
    mono[0] += 61
    with pytest.raises(ObservationError, match="DEADLINE"):
        with open_run(setup):
            pass
    now[0] -= timedelta(seconds=61)
    (root / "sample-run" / "run.json").unlink()
    with pytest.raises(ObservationError, match="LEGACY_OR_INCOMPLETE"):
        with open_run(setup):
            pass


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "permissions", "corrupt"])
def test_resume_rejects_unsafe_or_corrupt_files(setup, attack):
    root, _, _, _ = setup
    with open_run(setup) as run:
        run.finish("interrupted")
    path = root / "sample-run" / "samples.jsonl"
    if attack == "permissions":
        path.chmod(0o644)
    elif attack == "corrupt":
        with path.open("a") as stream:
            stream.write("not-json\n")
    else:
        other = root / "original"
        path.rename(other)
        if attack == "symlink":
            path.symlink_to(other)
        else:
            os.link(other, path)
    with pytest.raises(ObservationError):
        with open_run(setup):
            pass


def test_total_capacity_includes_previous_segments(setup):
    root, contract, _, _ = setup
    with open_run(setup) as run:
        run.finish("interrupted")
    path = root / "sample-run" / "samples.jsonl"
    with path.open("ab") as stream:
        stream.write(b" " * (contract["max_bytes"] - path.stat().st_size))
    with pytest.raises(lifecycle.SizeLimitError):
        with open_run(setup):
            pass


def test_writer_context_does_not_invent_clock_synchronization(setup):
    root, _, _, _ = setup
    with open_run(setup) as run:
        run.finish("interrupted")
    row = json.loads((root / "sample-run" / "samples.jsonl").read_text())
    assert row["schema_version"] == 1 and row["role"] == "ingress"
    assert row["clock_sync"] == "unknown" and row["clock_offset_ms"] is None
    assert len(row["producer_digest"]) == 64
