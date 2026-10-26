"""Owner-local CLI; reports are read-only, collection requires an explicit lease."""

import functools
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import typer

from wayvia.observation_run import ObservationError, read_private, request_stop
from wayvia.telemetry.records import EvidenceError, Policy
from wayvia.telemetry.report import export_bundle, timeline, verify_bundle
from wayvia.telemetry.store import DiagnosticStore


def register(app, guarded, output):
    base_guard = guarded

    def evidence_guard(function):
        @functools.wraps(function)
        def bounded_errors(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except (
                ObservationError,
                RecursionError,
                TypeError,
                KeyError,
                AttributeError,
                sqlite3.Error,
            ):
                # Malformed private inputs must not reach Typer's traceback with
                # raw JSON/SQL/log values in locals.
                raise EvidenceError("EVIDENCE_INPUT_INVALID") from None

        return base_guard(bounded_errors)

    guarded = evidence_guard
    diagnostics = typer.Typer(
        no_args_is_help=True, help="Private passive diagnostics and incident evidence"
    )
    app.add_typer(diagnostics, name="diagnostics")

    @diagnostics.command("collect")
    @guarded
    def collect(
        run_id: str = typer.Option(..., "--run-id"),
        until: str = typer.Option(..., "--until"),
        role: str = typer.Option(..., "--role"),
        generation: str = typer.Option(..., "--generation"),
        peer: str = typer.Option(..., "--peer"),
        port: int = typer.Option(..., "--port"),
        interface: str = typer.Option(..., "--interface"),
        path_id: str = typer.Option(..., "--path-id"),
        resume_interrupted: bool = False,
        policy_file: Path | None = None,
        audit_db: Path | None = None,
        trigger_inbox: Path | None = None,
        trigger_outbox: Path | None = None,
    ):
        """Explicit start only; no automatic installation, renewal or remote access."""
        from wayvia.telemetry.collector import run_collector

        policy = Policy.model_validate(read_private(policy_file)) if policy_file else Policy()
        try:
            code = run_collector(
                run_id=run_id,
                until=until,
                role=role,
                generation=generation,
                peer=peer,
                port=port,
                interface=interface,
                path_id=path_id,
                policy=policy,
                resume=resume_interrupted,
                audit_db=audit_db,
                inbox=trigger_inbox,
                outbox=trigger_outbox,
            )
        except ObservationError:
            raise EvidenceError("EVIDENCE_LIFECYCLE_REJECTED") from None
        output({"status": "ended", "exit_code": code})
        if code:
            raise typer.Exit(code)

    @diagnostics.command("stop")
    @guarded
    def stop(run_id: str):
        """Persist user stop; never starts or resumes a collector."""
        try:
            output(request_stop(Path("/var/lib/wayvia/diagnostics"), run_id))
        except ObservationError:
            raise EvidenceError("EVIDENCE_LIFECYCLE_REJECTED") from None

    @diagnostics.command("timeline")
    @guarded
    def query(
        source: list[Path] = typer.Option(..., "--source"),
        since: str | None = None,
        until: str | None = None,
        incident: str | None = None,
        timezone: str = "UTC",
        device: str | None = None,
        layer: str | None = None,
        role: str | None = None,
    ):
        output(
            timeline(
                source,
                start=since,
                end=until,
                incident_id=incident,
                timezone=timezone,
                device_id=device,
                layer=layer,
                role=role,
            )
        )

    @diagnostics.command("export")
    @guarded
    def export(
        source: list[Path] = typer.Option(..., "--source"),
        destination: Path = typer.Option(..., "--output"),
        since: str | None = None,
        until: str | None = None,
        incident: str | None = None,
        timezone: str = "UTC",
        device: str | None = None,
        layer: str | None = None,
        role: str | None = None,
        retain_seconds: int = 604800,
    ):
        report = timeline(
            source,
            start=since,
            end=until,
            incident_id=incident,
            timezone=timezone,
            device_id=device,
            layer=layer,
            role=role,
        )
        output(export_bundle(report, destination, retain_seconds=retain_seconds))

    @diagnostics.command("verify")
    @guarded
    def verify(bundle: Path):
        output(verify_bundle(bundle))

    @diagnostics.command("triggers")
    @guarded
    def triggers(source: Path):
        # Opening a writable store is unnecessary for this query.
        from wayvia.telemetry.records import stamp
        from wayvia.telemetry.store import read_database

        with read_database(source) as (connection, metadata):
            result = [
                {
                    "schema_version": 1,
                    "path_id": metadata["path_id"],
                    "revision": metadata["revision"],
                    "incident_id": r["id"],
                    "at": stamp(r["trigger_at"]),
                    "start": stamp(r["start"]),
                    "end": stamp(r["end"]),
                    "origin_role": r["origin_role"],
                    "origin_generation": r["origin_generation"],
                }
                for r in connection.execute("SELECT * FROM incidents ORDER BY trigger_at")
            ]
        output(result)

    @diagnostics.command("import-trigger")
    @guarded
    def import_trigger(source: Path, trigger_file: Path):
        store = DiagnosticStore.open(source)
        store.import_trigger(read_private(trigger_file), now=datetime.now(UTC).timestamp())
        output({"status": "pinned", "sampling_started": False})

    @diagnostics.command("ingest")
    @guarded
    def ingest(source: Path, events: Path, authorize_client_logs: bool = False):
        """Import explicitly provided typed events; raw logs are never copied."""
        from wayvia.observation_run import private

        private(events)
        if events.stat().st_size > 1048576:
            raise EvidenceError("EVIDENCE_IMPORT_TOO_LARGE")
        rows = [json.loads(line) for line in events.read_text().splitlines()]
        store = DiagnosticStore.open(source)
        # Project/authorize the entire file first. Idempotent inserts commit per row.
        from wayvia.telemetry.records import project

        clean = [project(row, client_authorized=authorize_client_logs) for row in rows]
        for row in clean:
            store.append(
                row, now=datetime.now(UTC).timestamp(), client_authorized=authorize_client_logs
            )
        output({"status": "ingested", "records": len(clean)})

    @diagnostics.command("prune")
    @guarded
    def prune(source: Path):
        DiagnosticStore.open(source).prune(datetime.now(UTC).timestamp())
        output({"status": "pruned", "sampling_started": False})
