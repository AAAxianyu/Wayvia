"""Unified client helpers. Inspection/staging never contact or control a client."""

from pathlib import Path

import typer

from wayvia.client import contract
from wayvia.client.measure import measure
from wayvia.client.profiles import inspect, private_json, read_profile, stage


def register(profile_app, guarded, output):
    @profile_app.command("capabilities")
    @guarded
    def capabilities():
        output(contract())

    @profile_app.command("inspect")
    @guarded
    def check(
        profile: Path,
        reference: Path | None = None,
        destination: Path | None = typer.Option(None, "--output"),
    ):
        result = inspect(read_profile(profile), read_profile(reference) if reference else None)
        output(private_json(result, destination) if destination else result)
        if result["errors"]:
            raise typer.Exit(5)

    @profile_app.command("stage")
    @guarded
    def update(candidate: Path, reference: Path, destination: Path, selected: str | None = None):
        output(
            stage(read_profile(candidate), destination, read_profile(reference), selected=selected)
        )

    @profile_app.command("diagnose")
    @guarded
    def diagnosis(
        profile: Path,
        reference: Path | None = None,
        server_status: Path | None = None,
        alerts: Path | None = None,
        destination: Path | None = typer.Option(None, "--output"),
    ):
        """Combine supplied private files only; does not fetch status or sample a network."""
        from wayvia.client.diagnostics import diagnose, read_status

        checked = inspect(read_profile(profile), read_profile(reference) if reference else None)
        result = diagnose(
            checked,
            read_status(server_status) if server_status else None,
            read_status(alerts) if alerts else None,
        )
        output(private_json(result, destination) if destination else result)
        if checked["errors"]:
            raise typer.Exit(5)

    @profile_app.command("measure")
    @guarded
    def measurement(
        kind: str = "latency",
        proxy_port: int = 7890,
        authorize_network: bool = False,
        max_bytes: int = 8388608,
        seconds: float = 10,
        destination: Path | None = typer.Option(None, "--output"),
    ):
        """One explicit bounded check through the local proxy; no direct fallback or daemon."""
        result = measure(
            kind=kind,
            proxy_port=proxy_port,
            authorized=authorize_network,
            max_bytes=max_bytes,
            seconds=seconds,
        )
        output(private_json(result, destination) if destination else result)
        if result["status"] != "measured":
            raise typer.Exit(5)
