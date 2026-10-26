"""One-shot alert operations; no background job is started by these commands."""

from datetime import UTC, datetime
from pathlib import Path

import typer

from wayvia.telemetry.alerts import AlertStore, load_config
from wayvia.telemetry.records import EvidenceError
from wayvia.telemetry.report import export_json


def register(app, guarded, output):
    alerts = typer.Typer(no_args_is_help=True, help="Independent private evidence alerts")
    app.add_typer(alerts, name="alerts")

    @alerts.command("evaluate")
    @guarded
    def evaluate(config: Path, state: Path):
        """Read declared sources once. Persist transitions without sending notifications."""
        result = AlertStore(state).evaluate(load_config(config), now=datetime.now(UTC).timestamp())
        output(result)

    @alerts.command("list")
    @guarded
    def listing(state: Path, destination: Path | None = typer.Option(None, "--output")):
        result = AlertStore(state, create=False).report(now=datetime.now(UTC).timestamp())
        output(export_json(result, destination) if destination else result)

    @alerts.command("watchdog")
    @guarded
    def watchdog(state: Path, max_age: int = 60):
        """Independent heartbeat check for an explicitly authorized external supervisor."""
        if not 10 <= max_age <= 3600:
            raise EvidenceError("ALERT_WATCHDOG_ARGUMENTS")
        result = AlertStore(state, create=False).report(now=datetime.now(UTC).timestamp())
        age = result["evaluation_age_seconds"]
        stale = age is None or age < 0 or age > max_age
        output(
            {
                "status": "stale" if stale else "fresh",
                "evaluation_age_seconds": age,
                "business_impact": "unknown",
                "next_action": (
                    "Check evaluator scheduling and storage; do not restart an ended observation."
                ),
            }
        )
        if stale:
            raise typer.Exit(5)

    @alerts.command("deliver")
    @guarded
    def deliver(config: Path, state: Path, authorize_send: bool = False):
        store = AlertStore(state, create=False)
        settings = load_config(config)
        store.validate_contract(settings)
        result = store.deliver(
            settings.channel,
            authorized=authorize_send,
            now=datetime.now(UTC).timestamp(),
        )
        output(result)
        if result["failed"] or result["expired_pending"]:
            raise typer.Exit(5)

    @alerts.command("discard-expired")
    @guarded
    def discard_expired(state: Path):
        """Explicitly remove unsent notifications older than the seven-day delivery TTL."""
        output(AlertStore(state, create=False).discard_expired(now=datetime.now(UTC).timestamp()))
