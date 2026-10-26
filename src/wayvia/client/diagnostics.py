"""Join explicit private files without contacting a client or a production host."""

import json
import time
from pathlib import Path

from wayvia.errors import WayviaError
from wayvia.evidence import safe_time, token
from wayvia.observation_run import ObservationError, private
from wayvia.services.diagnostics import _fresh, transaction_diagnostic
from wayvia.telemetry.records import stamp, timestamp


def read_status(path):
    path = Path(path).absolute()
    try:
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError()
        private(path)
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError()
        result = json.loads(path.read_text())
        if not isinstance(result, (dict, list)):
            raise ValueError()
        return result
    except (OSError, ValueError, TypeError, RecursionError, ObservationError):
        raise WayviaError(
            "CLIENT_STATUS_INVALID", "Use a valid bounded private status file"
        ) from None


def diagnose(profile_report, server_status=None, alerts=None, *, now=None):
    now = time.time() if now is None else now
    values = server_status.get("dataplane") if isinstance(server_status, dict) else server_status
    rows = values if isinstance(values, list) and len(values) <= 64 else []
    roles = []
    seen = set()
    ambiguous = False
    for tx in rows:
        if (
            not isinstance(tx, dict)
            or tx.get("current") is not True
            or not isinstance(tx.get("role"), str)
            or tx.get("role") not in {"ingress", "exit"}
        ):
            continue
        role = tx["role"]
        if role in seen:
            ambiguous = True
        seen.add(role)
        fresh = _fresh(tx.get("monitored_at"), tx.get("monitor_valid_until"), now)
        result = transaction_diagnostic(tx, now=now) if fresh else {}
        roles.append(
            {
                "role": role,
                "status": result.get("status", "unknown"),
                "revision": token(tx.get("revision"), r"[a-f0-9]{64}"),
                "generation": token(tx.get("generation"), r"[a-f0-9]{32}"),
                "observed_at": safe_time(tx.get("monitored_at")),
                "fresh": fresh,
                "scope": result.get("verification_scope", "unknown"),
            }
        )
    if ambiguous:
        for row in roles:
            row["status"] = "unknown"
    server = (
        "failed"
        if any(r["status"] == "failed" for r in roles)
        else "healthy"
        if any(r["role"] == "ingress" and r["status"] == "healthy" for r in roles)
        else "component_ready"
        if any(r["status"] == "ready" for r in roles)
        else "unknown"
    )
    alert_refs = []
    alerts_fresh = False
    if isinstance(alerts, dict):
        at = safe_time(alerts.get("generated_at"))
        alerts_fresh = bool(at and 0 <= now - timestamp(at) <= 60)
        if alerts_fresh:
            items = alerts.get("alerts", [])
            for alert in items[:128] if isinstance(items, list) else []:
                if (
                    not isinstance(alert, dict)
                    or not isinstance(alert.get("category"), str)
                    or alert.get("category")
                    not in {
                        "path_health",
                        "observation_integrity",
                    }
                ):
                    continue
                # A report from another deployment/run must not be attributed to this client.
                evidence = alert.get("evidence", [])
                if not isinstance(evidence, list) or not any(
                    isinstance(e, dict)
                    and any(
                        e.get("role") == r["role"]
                        and e.get("revision") == r["revision"]
                        and e.get("generation") == r["generation"]
                        and r["generation"]
                        for r in roles
                    )
                    for e in evidence[:8]
                ):
                    continue
                ident = token(alert.get("alert_id"), r"[a-f0-9]{64}")
                if ident:
                    alert_refs.append(
                        {
                            "alert_id": ident,
                            "incident_id": token(alert.get("incident_id"), r"[a-f0-9]{32}"),
                            "category": alert["category"],
                            "status": alert.get("status")
                            if isinstance(alert.get("status"), str)
                            and alert.get("status") in {"open", "resolved", "ended"}
                            else "unknown",
                        }
                    )
    return {
        "schema_version": 1,
        "generated_at": stamp(now),
        "profile": profile_report,
        "server_observation": server,
        "roles": roles,
        "alert_references": alert_refs,
        "alerts_report_fresh": alerts_fresh,
        "client_connectivity": "unknown",
        "business_impact": "unknown",
        "automatic_actions": [],
        "next_actions": [
            (
                "Compare effective Verge configuration with the trusted subscription; "
                "inspect local service/TUN permissions."
            ),
            (
                "Use matching incident IDs and generations with diagnostics timeline; "
                "stale or missing evidence stays unknown."
            ),
            (
                "Keep subscription, local configuration and server findings separate; "
                "no automatic restart or direct fallback."
            ),
        ],
    }
