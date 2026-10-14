import json

import pytest

from wayvia.adapters.base import AdapterError, CommandResult
from wayvia.adapters.linux import LinuxExecutor
from wayvia.agent.models import Action


def nft_ruleset(
    *,
    packets=1,
    bytes_=48,
    handle_offset=0,
    verdict="drop",
    port=1080,
    policy="accept",
    rules=None,
):
    entries = [
        {"metainfo": {"version": "1.0.9", "json_schema_version": 1}},
        {"table": {"family": "inet", "name": "wayvia_guard", "handle": 1 + handle_offset}},
        {
            "chain": {
                "family": "inet",
                "table": "wayvia_guard",
                "name": "input",
                "type": "filter",
                "hook": "input",
                "prio": -10,
                "policy": policy,
                "handle": 2 + handle_offset,
            }
        },
    ]
    rule_specs = rules or [(port, verdict)]
    for index, (rule_port, rule_verdict) in enumerate(rule_specs):
        entries.append(
            {
                "rule": {
                    "family": "inet",
                    "table": "wayvia_guard",
                    "chain": "input",
                    "expr": [
                        {
                            "match": {
                                "op": "==",
                                "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                                "right": rule_port,
                            }
                        },
                        {"counter": {"packets": packets, "bytes": bytes_}},
                        {rule_verdict: None},
                    ],
                    "handle": 3 + handle_offset + index,
                }
            }
        )
    return json.dumps({"nftables": entries})


def nft_exit_ruleset(*, subnet="10.8.0.0/24", interface="eth0", packets=1, bytes_=48):
    return json.dumps(
        {
            "nftables": [
                {"metainfo": {"json_schema_version": 1}},
                {"table": {"family": "inet", "name": "wayvia_exit", "handle": 10}},
                {
                    "chain": {
                        "family": "inet",
                        "table": "wayvia_exit",
                        "name": "postrouting",
                        "type": "nat",
                        "hook": "postrouting",
                        "prio": 100,
                        "policy": "accept",
                        "handle": 11,
                    }
                },
                {
                    "rule": {
                        "family": "inet",
                        "table": "wayvia_exit",
                        "chain": "postrouting",
                        "expr": [
                            {
                                "match": {
                                    "op": "==",
                                    "left": {"payload": {"protocol": "ip", "field": "saddr"}},
                                    "right": {
                                        "prefix": {
                                            "addr": subnet.split("/")[0],
                                            "len": int(subnet.split("/")[1]),
                                        }
                                    },
                                }
                            },
                            {
                                "match": {
                                    "op": "==",
                                    "left": {"meta": {"key": "oifname"}},
                                    "right": interface,
                                }
                            },
                            {"counter": {"packets": packets, "bytes": bytes_}},
                            {"masquerade": None},
                        ],
                        "handle": 12,
                    }
                },
            ]
        }
    )


class NftRunner:
    def __init__(
        self,
        nft_json,
        *,
        routes='[{"dst":"default","dev":"eth0"}]',
        nft_rc=0,
        tables_json=None,
    ):
        self.nft_json = nft_json
        self.routes = routes
        self.nft_rc = nft_rc
        self.tables_json_override = tables_json
        self.calls = []
        self.full_snapshot = "table inet wayvia_guard { counter packets 2 bytes 96 drop }\n"

    def _tables_json(self):
        if self.tables_json_override is not None:
            return self.tables_json_override
        try:
            entries = json.loads(self.nft_json).get("nftables", [])
        except (json.JSONDecodeError, AttributeError):
            entries = [{"table": {"family": "inet", "name": "wayvia_guard"}}]
        tables = [
            entry["table"]
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("table"), dict)
        ]
        return json.dumps(
            {
                "nftables": [{"metainfo": {"json_schema_version": 1}}]
                + [{"table": table} for table in tables]
            }
        )

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[:2] == ["nft", "-j"] and argv[2:] == ["list", "tables", "inet"]:
            return CommandResult(self.nft_rc, self._tables_json(), "nftables read failed")
        if argv[:2] == ["nft", "-j"] and argv[2:5] == ["list", "table", "inet"]:
            return CommandResult(self.nft_rc, self.nft_json, "nftables read failed")
        if argv[:2] == ["ip", "-j"]:
            return CommandResult(0, self.routes)
        if argv[:3] == ["nft", "list", "table"]:
            return CommandResult(0, self.full_snapshot)
        raise AssertionError(f"unexpected command: {argv}")


def executor(runner):
    return LinuxExecutor(runner=runner, enforce_environment=False)


def guard_action():
    return Action(kind="guard", resource="wayvia_guard")


def test_baseline_ignores_only_nft_counter_values_and_handles():
    runner = NftRunner(nft_ruleset(packets=1, bytes_=48))
    target = executor(runner)
    before = target.baseline([guard_action()])

    runner.nft_json = nft_ruleset(packets=37, bytes_=2048, handle_offset=100)

    assert target.baseline([guard_action()]) == before


def test_baseline_keeps_nested_handle_fields():
    snapshot = {
        "rule": {
            "handle": 3,
            "expr": [{"extension": {"handle": "meaningful-value"}}],
        }
    }

    assert LinuxExecutor._normalize_nft_baseline(snapshot) == {
        "rule": {"expr": [{"extension": {"handle": "meaningful-value"}}]}
    }


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (nft_ruleset(verdict="drop"), nft_ruleset(verdict="accept")),
        (nft_ruleset(port=1080), nft_ruleset(port=1081)),
        (nft_ruleset(policy="accept"), nft_ruleset(policy="drop")),
    ],
)
def test_baseline_detects_firewall_rule_changes(before, after):
    runner = NftRunner(before)
    target = executor(runner)
    before_digest = target.baseline([guard_action()])

    runner.nft_json = after

    assert target.baseline([guard_action()]) != before_digest


def test_baseline_preserves_rule_order():
    before = nft_ruleset(rules=[(1080, "accept"), (1081, "drop")])
    after = nft_ruleset(rules=[(1081, "drop"), (1080, "accept")])
    runner = NftRunner(before)
    target = executor(runner)
    before_digest = target.baseline([guard_action()])

    runner.nft_json = after

    assert target.baseline([guard_action()]) != before_digest


def test_baseline_detects_route_changes():
    runner = NftRunner(nft_ruleset())
    target = executor(runner)
    before = target.baseline([guard_action()])

    runner.routes = '[{"dst":"default","dev":"eth1"}]'

    assert target.baseline([guard_action()]) != before


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (nft_exit_ruleset(subnet="10.8.0.0/24"), nft_exit_ruleset(subnet="10.9.0.0/24")),
        (nft_exit_ruleset(interface="eth0"), nft_exit_ruleset(interface="eth1")),
    ],
)
def test_baseline_detects_exit_nat_changes(before, after):
    target = executor(NftRunner(before))
    action = Action(
        kind="exit_nat",
        resource="wayvia_exit",
        subnet="10.8.0.0/24",
        interface="eth0",
    )
    before_digest = target.baseline([action])
    target.runner.nft_json = after

    assert target.baseline([action]) != before_digest


def test_inspect_action_keeps_full_nft_snapshot_for_rollback():
    runner = NftRunner(nft_ruleset())
    snapshot = executor(runner).inspect_action(guard_action())

    assert snapshot["rules"] == runner.full_snapshot
    assert "packets 2 bytes 96" in snapshot["rules"]
    assert runner.calls[0][0] == ["nft", "list", "table", "inet", "wayvia_guard"]


@pytest.mark.parametrize(
    "output",
    [
        "not-json",
        "{}",
        '{"nftables": {}}',
        '{"nftables": [{"rule": {"expr": []}}]}',
        json.dumps(
            {
                "nftables": [
                    {"table": {"family": "inet", "name": "wayvia_guard"}},
                    {
                        "rule": {
                            "family": "inet",
                            "table": "wayvia_guard",
                            "chain": "input",
                            "expr": [{"counter": {"packets": -1, "bytes": 1}}],
                        }
                    },
                ]
            }
        ),
    ],
)
def test_invalid_nft_json_fails_baseline_closed(output):
    tables_json = json.dumps(
        {
            "nftables": [
                {"metainfo": {"json_schema_version": 1}},
                {"table": {"family": "inet", "name": "wayvia_guard"}},
            ]
        }
    )
    with pytest.raises(AdapterError) as err:
        executor(NftRunner(output, tables_json=tables_json)).baseline([guard_action()])

    assert err.value.code == "BASELINE_INVALID"


def test_nftables_command_failure_fails_baseline_closed():
    with pytest.raises(AdapterError) as err:
        executor(NftRunner("", nft_rc=1)).baseline([guard_action()])

    assert err.value.code == "BASELINE_READ_FAILED"


def test_absent_wayvia_table_is_a_valid_initial_baseline():
    runner = NftRunner('{"nftables": [{"metainfo": {"json_schema_version": 1}}]}')

    assert executor(runner).baseline([guard_action()])
