"""Independent systemd timer/reboot entry point; does not require the API."""

import argparse
import json

from wayvia.adapters.linux import LinuxExecutor
from wayvia.agent.transactions import TransactionManager

SLOW_MS = 2000
SUMMARY = ("id", "role", "revision", "state", "generation", "current")
SUMMARY += ("monitored_at", "monitor_valid_until")
EVIDENCE = ("layer", "status", "kind", "check", "error", "duration_ms", "observed_at", "event_id")


def compact(row):
    """Healthy, fast monitor rounds keep per-layer timing only; anything else stays full."""
    evidence = row.get("evidence") or []
    if (
        row.get("state") != "ACTIVE"
        or row.get("soft_failures")
        or any(
            item.get("status") != "healthy" or (item.get("duration_ms") or 0) > SLOW_MS
            for item in evidence
        )
    ):
        return row
    return {
        **{key: row[key] for key in SUMMARY if key in row},
        "evidence": [
            {key: item[key] for key in EVIDENCE if item.get(key) is not None} for item in evidence
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal-dir", default="/var/lib/wayvia/agent")
    parser.add_argument("--config-dir", default="/etc/wayvia")
    parser.add_argument("--unit-dir", default="/etc/systemd/system")
    parser.add_argument("--transaction")
    parser.add_argument("--rebuild-active", action="store_true")
    parser.add_argument("--monitor", action="store_true")
    args = parser.parse_args()
    executor = LinuxExecutor(
        state_dir=args.journal_dir, config_dir=args.config_dir, unit_dir=args.unit_dir
    )
    executor.require_environment()
    manager = TransactionManager(executor, args.journal_dir)
    if args.transaction:
        result = manager.rollback(args.transaction, only_unconfirmed=True)
    elif args.monitor:
        result = [
            manager.monitor(t["id"])
            for t in manager.status()
            if t["state"] in {"ACTIVE", "DEGRADED"}
        ]
    else:
        result = manager.recover(rebuild_active=args.rebuild_active)
    if args.monitor:
        print(json.dumps([compact(row) for row in result], separators=(",", ":")))
    else:
        print(json.dumps(result))
    rows = result if isinstance(result, list) else [result]
    return 5 if any(r["state"] in {"RECOVERY_REQUIRED", "DEGRADED"} for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
