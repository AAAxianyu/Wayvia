"""Upgrade acceptance compares persistent identities and cumulative counters."""

import copy
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "upgrade", Path(__file__).parents[2] / "deploy/upgrade.py"
)
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


def snapshot():
    return {
        "identity_sha256": "devices-tokens-publications-title",
        "device_count": 2,
        "subscription_count": 5,
        "control_config_sha256": "configuration",
        "control_secrets": {"phone": "credential", "request": "replay"},
        "static_tls_secrets": {"tls.key": "private-key"},
        "usage": [["phone", 123, 456]],
        "schema": [1, 2, 3],
    }


def test_migration_and_new_history_do_not_invalidate_retained_state():
    before = snapshot()
    after = copy.deepcopy(before)
    after["schema"].append(4)
    after["usage"][0][2] += 1
    assert upgrade.retained(before, after)
    assert before["schema"] == [1, 2, 3]


@pytest.mark.parametrize(
    "field", ["identity_sha256", "control_secrets", "static_tls_secrets", "control_config_sha256"]
)
def test_credential_configuration_or_publication_changes_are_rejected(field):
    before = snapshot()
    after = copy.deepcopy(before)
    after[field] = "changed"
    assert not upgrade.retained(before, after)


def test_lower_or_missing_cumulative_totals_are_rejected():
    before = snapshot()
    after = copy.deepcopy(before)
    after["usage"][0][1] -= 1
    assert not upgrade.retained(before, after)
    after["usage"] = []
    assert not upgrade.retained(before, after)


def test_archive_prefix_reduction_keeps_unrelated_siblings_separate():
    assert upgrade.reduced_paths(
        [
            "var/lib/wayvia",
            "var/lib/wayvia/control/metadata.db",
            "var/lib/wayvia-other/file",
            "opt/wayvia/.venv",
            "opt/wayvia/.venv/bin/python",
        ]
    ) == ["opt/wayvia/.venv", "var/lib/wayvia", "var/lib/wayvia-other/file"]
