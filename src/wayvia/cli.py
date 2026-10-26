"""Ordinary-user CLI. Network writes are delegated to the restricted local agent."""

import functools
import json
import os
import secrets
from pathlib import Path

import typer
import yaml
from pydantic import ValidationError

from wayvia import __version__
from wayvia.client.commands import register as register_client
from wayvia.errors import ExitCode, WayviaError
from wayvia.telemetry.commands import register as register_diagnostics

app = typer.Typer(no_args_is_help=True, help="Wayvia split-routing gateway control plane")
device_app = typer.Typer(no_args_is_help=True)
subscription_app = typer.Typer(no_args_is_help=True)
policy_app = typer.Typer(no_args_is_help=True)
profile_app = typer.Typer(no_args_is_help=True)
secret_app = typer.Typer(no_args_is_help=True)
rules_app = typer.Typer(no_args_is_help=True)
config_app = typer.Typer(no_args_is_help=True)
app.add_typer(device_app, name="device")
app.add_typer(subscription_app, name="subscription")
app.add_typer(policy_app, name="policy")
app.add_typer(profile_app, name="profile")
app.add_typer(secret_app, name="secret")
app.add_typer(rules_app, name="rules")
app.add_typer(config_app, name="config")


def output(value):
    typer.echo(json.dumps(value, ensure_ascii=False, default=str))


def guarded(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except WayviaError as exc:
            output(exc.as_dict())
            code = (
                ExitCode.ENVIRONMENT
                if exc.code
                in {
                    "ENVIRONMENT_UNSUPPORTED",
                    "COMPONENT_MISSING",
                    "PEER_AUTH_UNSUPPORTED",
                    "VERSION_MISMATCH",
                    "ISOLATION_REQUIRED",
                }
                else exc.exit_code
            )
            raise typer.Exit(int(code)) from exc
        except ValidationError as exc:
            output(
                {
                    "code": "validation_error",
                    "message": "Invalid fields",
                    "details": [
                        {"path": list(item["loc"]), "code": item["type"]} for item in exc.errors()
                    ],
                }
            )
            raise typer.Exit(int(ExitCode.INPUT)) from exc
        except (OSError, ValueError) as exc:
            output({"code": "operation_failed", "message": type(exc).__name__, "details": {}})
            raise typer.Exit(int(ExitCode.RUNTIME)) from exc

    return wrapper


register_diagnostics(app, guarded, output)
register_client(profile_app, guarded, output)


@app.callback(invoke_without_command=True)
@guarded
def options(
    ctx: typer.Context,
    data_dir: Path | None = None,
    config: Path | None = None,
    mode: str = "production",
    agent_socket: Path | None = None,
    system: bool = typer.Option(
        False, "--system", help="Use the installed wayvia service identity and control directory"
    ),
    version: bool = typer.Option(False, "--version", is_eager=True),
):
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    if system:
        import platform
        import pwd

        if (
            data_dir is not None
            or config is not None
            or agent_socket is not None
            or mode != "production"
        ):
            raise WayviaError(
                "system_options_conflict",
                "--system fixes production mode, directory, configuration and socket",
            )
        if platform.system() != "Linux":
            raise WayviaError(
                "system_environment",
                "Installed system mode requires Linux",
                exit_code=ExitCode.ENVIRONMENT,
            )
        try:
            service_uid = pwd.getpwnam("wayvia").pw_uid
        except KeyError:
            raise WayviaError(
                "system_not_installed",
                "The wayvia service identity does not exist",
                exit_code=ExitCode.ENVIRONMENT,
            ) from None
        if os.geteuid() != service_uid:
            raise WayviaError(
                "system_identity_required",
                "Run system control commands as the wayvia service account",
                exit_code=ExitCode.AUTH,
            )
        data_dir = Path("/var/lib/wayvia/control")
    data_dir = data_dir or Path(".wayvia")
    agent_socket = agent_socket or Path("/run/wayvia/agent.sock")
    ctx.obj = {
        "root": data_dir,
        "config": config or data_dir / "config.yaml",
        "mode": mode,
        "socket": agent_socket,
    }
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(ExitCode.INPUT)


def configuration(ctx):
    from wayvia.config import load_config

    return load_config(ctx.obj["config"])


@config_app.command("publish")
@guarded
def publish_configuration(
    ctx: typer.Context, exit_status: Path = typer.Option(..., "--exit-status")
):
    """Publish after both roles confirm; import exit status via a trusted admin channel."""
    output(service(ctx).publish(json.loads(exit_status.read_text())))


@config_app.command("publication")
@guarded
def show_publication(ctx: typer.Context):
    output(service(ctx).publication())


def service(ctx):
    from wayvia.services.control import AgentDataplane, ControlService, SimulatedDataplane

    mode = ctx.obj["mode"]
    dataplane = SimulatedDataplane() if mode == "demo" else AgentDataplane(ctx.obj["socket"])
    return ControlService(ctx.obj["root"], configuration(ctx), mode=mode, dataplane=dataplane)


@app.command()
@guarded
def init(ctx: typer.Context, directory: Path | None = None):
    from wayvia.config import example_config
    from wayvia.secrets import SecretStore
    from wayvia.storage import Database

    root = directory or ctx.obj["root"]
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "config.yaml"
    if path.exists():
        raise WayviaError("already_initialized", "Configuration already exists")
    path.write_text(yaml.safe_dump(example_config(), sort_keys=False, allow_unicode=True))
    path.chmod(0o600)
    store = SecretStore(root / "secrets")
    store.write("admin-token", secrets.token_urlsafe(48))
    Database(root / "metadata.db")
    output(
        {
            "status": "initialized",
            "config": str(path),
            "admin_token_file": str(root / "secrets/admin-token"),
            "mode": ctx.obj["mode"],
        }
    )


@app.command()
@guarded
def validate(config: Path):
    from wayvia.config import load_config

    model = load_config(config)
    output({"status": "valid", "schema_version": model.schema_version})


@policy_app.command("explain")
@guarded
def explain(
    ctx: typer.Context,
    domain: str = typer.Argument(""),
    ip: str | None = None,
    protocol: str = "tcp",
    port: int = 443,
):
    from wayvia.policy import PolicyCompiler

    output(
        PolicyCompiler(configuration(ctx)).explain(
            domain=domain or None, ip=ip, protocol=protocol, port=port
        )
    )


@device_app.command("add")
@guarded
def add(ctx: typer.Context, name: str, platform: str = "android"):
    output(service(ctx).add(name, platform))


@device_app.command("list")
@guarded
def list_devices(ctx: typer.Context):
    output({"devices": service(ctx).devices()})


@device_app.command("usage")
@guarded
def device_usage(ctx: typer.Context):
    """Observed authenticated connections and proxy payload totals (not billing)."""
    output(
        {
            "devices": [
                {key: device[key] for key in ("id", "name", "platform", "status", "usage")}
                for device in service(ctx).devices()
            ]
        }
    )


@app.command("usage-collect")
@guarded
def usage_collect(ctx: typer.Context, once: bool = False):
    """Collect every five seconds; reads the fixed local agent operation only."""
    import time

    control = service(ctx)
    while True:
        started = time.monotonic()
        result = control.collect_usage()
        if once:
            output(result)
            if result["status"] != "observed":
                raise typer.Exit(int(ExitCode.RUNTIME))
            return
        if result["status"] != "observed":
            output(result)
        time.sleep(max(0.1, 5 - (time.monotonic() - started)))


@device_app.command("history")
@guarded
def device_history(
    ctx: typer.Context,
    device_id: str,
    since: float,
    until: float,
    grain: str = "minute",
    destination: Path | None = typer.Option(None, "--output"),
):
    """Private account usage history. Times are Unix seconds; missing buckets stay unknown."""
    from wayvia.services.usage_history import history
    from wayvia.telemetry.report import export_json

    result = history(service(ctx).db, device_id, start=since, end=until, grain=grain)
    output(export_json(result, destination) if destination else result)


@device_app.command("history-prune")
@guarded
def device_history_prune(ctx: typer.Context):
    """Apply minute/hour retention without contacting the dataplane."""
    import time

    from wayvia.services.usage_history import prune

    control = service(ctx)
    with control.db.change_lock(), control.db.connect() as connection:
        prune(connection, time.time())
    output({"status": "pruned", "sampling_started": False})


@device_app.command("rotate")
@guarded
def rotate(ctx: typer.Context, device: str):
    output(service(ctx).rotate(device))


@device_app.command("revoke")
@guarded
def revoke_device(ctx: typer.Context, device: str):
    output(service(ctx).revoke_device(device))


@device_app.command("reconcile")
@guarded
def reconcile(ctx: typer.Context):
    result = service(ctx).reconcile()
    output(result)
    if result.get("status") != "active":
        raise typer.Exit(ExitCode.RUNTIME)


@profile_app.command("export")
@guarded
def export(
    ctx: typer.Context, device: str, format: str = "sing-box", output_file: Path | None = None
):
    text = service(ctx).export(device, format)
    if output_file:
        output_file.write_text(text)
        output_file.chmod(0o600)
        output({"status": "exported", "path": str(output_file), "format": format})
    else:
        typer.echo(text)


@subscription_app.command("issue")
@guarded
def issue(
    ctx: typer.Context,
    device: str,
    format: str = "sing-box",
    lifetime: int = 2592000,
    base_url: str | None = None,
):
    from wayvia.client import import_link, subscription_url

    if base_url:
        # Validate before issuing a credential; bad input must not create orphan links.
        subscription_url(base_url, "/subscriptions/" + "x" * 64 + "/mihomo")
        if format != "mihomo":
            raise WayviaError("CLIENT_FORMAT_REQUIRED", "One-click import requires Mihomo format")
    result = service(ctx).issue(device, format, lifetime)
    if base_url:
        result["url"] = subscription_url(base_url, result["path"])
        result["import_url"] = import_link(result["url"])
    output(result)


@subscription_app.command("import-link")
@guarded
def subscription_import_link(url: str):
    """Encode an existing private HTTPS Mihomo link without contacting it."""
    from wayvia.client import import_link

    output({"import_url": import_link(url), "contains_device_credential": True})


@subscription_app.command("info")
@guarded
def subscription_info(ctx: typer.Context, token_file: Path):
    """Read local device usage/expiry using a private token file; no subscription download."""
    from wayvia.observation_run import ObservationError, private

    try:
        private(token_file)
    except ObservationError:
        raise WayviaError("CLIENT_TOKEN_INVALID", "A private token file is required") from None
    if token_file.stat().st_size > 256:
        raise WayviaError("CLIENT_TOKEN_INVALID", "Invalid token file")
    output(service(ctx).subscription_info(token_file.read_text().strip()))


@subscription_app.command("name")
@guarded
def name_subscription(ctx: typer.Context, subscription: str, name: str):
    """Set the download display name without rotating credentials or reloading ingress."""
    from wayvia.client.names import set_display_name

    output(set_display_name(service(ctx).db, subscription, name))


@subscription_app.command("revoke")
@guarded
def revoke_subscription(ctx: typer.Context, subscription: str):
    """Revoke download permission only. Use device revoke to also cut off traffic."""
    output(service(ctx).revoke_subscription(subscription))


@subscription_app.command("list")
@guarded
def list_subscriptions(ctx: typer.Context):
    """List subscription metadata and fetch times without bearer tokens."""
    output({"subscriptions": service(ctx).subscriptions()})


@secret_app.command("import")
@guarded
def import_secret(ctx: typer.Context, reference: str, source: Path):
    from wayvia.secrets import SecretStore

    SecretStore(ctx.obj["root"] / "secrets").write(reference, source.read_text())
    output({"status": "stored", "secret_ref": reference})


@rules_app.command("import-apnic")
@guarded
def import_apnic(source: Path, sha256: str, output_file: Path):
    from wayvia.config import RuleSetImporter

    rule_set = RuleSetImporter.from_apnic(source, expected_sha256=sha256)
    output_file.write_text(yaml.safe_dump(rule_set.model_dump(mode="json"), sort_keys=False))
    output(
        {
            "status": "imported",
            "version": rule_set.version,
            "cidrs": len(rule_set.cidrs),
            "path": str(output_file),
            "digest": rule_set.content_digest,
        }
    )


@rules_app.command("import-domains")
@guarded
def import_domains(source: Path, sha256: str, provenance: str, version: str, output_file: Path):
    from wayvia.config import RuleSetImporter

    rule_set = RuleSetImporter.from_domains(
        source, source=provenance, version=version, expected_sha256=sha256
    )
    output_file.write_text(yaml.safe_dump(rule_set.model_dump(mode="json"), sort_keys=False))
    output(
        {
            "status": "imported",
            "version": rule_set.version,
            "domains": len(rule_set.domains),
            "path": str(output_file),
            "digest": rule_set.content_digest,
        }
    )


@rules_app.command("import-dnsmasq")
@guarded
def import_dnsmasq(source: Path, sha256: str, provenance: str, version: str, output_file: Path):
    from wayvia.config import RuleSetImporter

    rules = RuleSetImporter.from_dnsmasq(
        source, source=provenance, version=version, expected_sha256=sha256
    )
    output_file.write_text(yaml.safe_dump(rules.model_dump(mode="json"), sort_keys=False))
    output(
        {
            "status": "imported",
            "version": rules.version,
            "domains": len(rules.domains),
            "path": str(output_file),
            "digest": rules.content_digest,
        }
    )


def artifacts(ctx, config, role):
    from wayvia.config import load_config
    from wayvia.render import ServerRenderer
    from wayvia.secrets import SecretStore
    from wayvia.storage import Database

    model = load_config(config)
    store = SecretStore(ctx.obj["root"] / "secrets")
    secret_values = {
        path.name: store.read(path.name)
        for path in store.root.iterdir()
        if path.name != "admin-token" and not path.name.startswith("request-")
    }
    with Database(ctx.obj["root"] / "metadata.db").connect() as connection:
        devices = [
            {"id": row["id"], "password": store.read(row["secret_ref"])}
            for row in connection.execute(
                "SELECT * FROM devices WHERE desired_action IS NOT 'revoke' AND status!='revoked'"
            )
        ]
    return ServerRenderer(model).render(role, devices, secret_values)


@app.command()
@guarded
def render(
    ctx: typer.Context,
    config: Path,
    role: str = typer.Option(..., "--role"),
    output: Path = typer.Option(..., "--output"),
):
    rendered = artifacts(ctx, config, role)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, text in rendered.items():
        path = output / name
        if path.is_symlink() or Path(name).name != name:
            raise WayviaError("unsafe_artifact", "Artifact filename is unsafe")
        path.write_text(text)
        path.chmod(0o600)
    typer.echo(json.dumps({"status": "rendered", "role": role, "files": sorted(rendered)}))


@app.command()
@guarded
def plan(
    ctx: typer.Context,
    config: Path,
    role: str = typer.Option(..., "--role"),
    output_file: Path = typer.Option(..., "--output-file"),
    baseline: str = "auto",
    probes: Path | None = None,
    exit_interface: str | None = None,
):
    from wayvia.agent.models import Probe
    from wayvia.agent.planner import build_plan
    from wayvia.config import load_config

    rendered = artifacts(ctx, config, role)
    selected = (
        [Probe.model_validate(item) for item in json.loads(probes.read_text())] if probes else []
    )
    result = build_plan(
        role=role,
        revision=load_config(config).revision,
        artifacts=rendered,
        baseline=baseline,
        probes=selected,
        exit_interface=exit_interface,
    )
    if baseline == "auto":
        actual = agent_call(ctx, "baseline", plan=result.model_dump(mode="json"))["baseline"]
        result = result.model_copy(update={"baseline": actual}).seal()
    output_file.write_text(result.model_dump_json(indent=2))
    output_file.chmod(0o600)
    output(
        {
            "status": "planned",
            "path": str(output_file),
            "plan_id": result.digest,
            "digest": result.digest,
        }
    )


def agent_call(ctx, operation, **data):
    from wayvia.agent.client import AgentClient

    return AgentClient(ctx.obj["socket"]).request({"operation": operation, **data})


@app.command()
@guarded
def apply(ctx: typer.Context, plan: Path):
    result = agent_call(ctx, "apply", plan=json.loads(plan.read_text()))
    output(result)
    if result.get("state") == "AWAITING_CONFIRM":
        raise typer.Exit(ExitCode.AWAITING_CONFIRM)
    if result.get("state") != "ACTIVE":
        raise typer.Exit(ExitCode.RUNTIME)


@app.command()
@guarded
def confirm(ctx: typer.Context, transaction: str):
    output(agent_call(ctx, "confirm", transaction_id=transaction))


@app.command()
@guarded
def rollback(ctx: typer.Context, transaction: str):
    result = agent_call(ctx, "rollback", transaction_id=transaction)
    output(result)
    if result.get("state") != "ROLLED_BACK":
        raise typer.Exit(ExitCode.RUNTIME)


@app.command()
@guarded
def status(ctx: typer.Context, json_output: bool = typer.Option(False, "--json")):
    result = {"version": __version__, "mode": ctx.obj["mode"], "devices": service(ctx).devices()}
    try:
        result["agent"] = agent_call(ctx, "status")
    except (OSError, WayviaError):
        result["agent"] = {"status": "unknown", "code": "agent_unavailable"}
    output(result)


@app.command()
@guarded
def doctor(ctx: typer.Context):
    from wayvia.services.diagnostics import diagnose

    result = diagnose(socket_path=ctx.obj["socket"])
    output(result)
    if not result["healthy"]:
        code = (
            ExitCode.ENVIRONMENT
            if any(layer["status"] == "unsupported" for layer in result["layers"])
            else ExitCode.RUNTIME
        )
        raise typer.Exit(code)


@app.command()
@guarded
def serve(
    ctx: typer.Context, host: str = "127.0.0.1", port: int = 8787, subscriptions_only: bool = False
):
    import ipaddress

    import uvicorn

    from wayvia.api import create_app
    from wayvia.secrets import SecretStore

    if not ipaddress.ip_address(host).is_loopback:
        raise WayviaError(
            "local_bind_required",
            "API must bind a loopback address; publish subscriptions via HTTPS proxy",
        )
    token = SecretStore(ctx.obj["root"] / "secrets").read("admin-token")
    uvicorn.run(
        create_app(service(ctx), token, subscriptions_only=subscriptions_only),
        host=host,
        port=port,
        access_log=False,
        log_level="warning",
    )


@app.command("agent")
@guarded
def agent(
    ctx: typer.Context,
    journal_dir: Path = Path("/var/lib/wayvia/agent"),
    artifacts_dir: Path = Path("/etc/wayvia"),
    allowed_uid: int = os.getuid(),
):
    from wayvia.agent.server import serve_agent

    serve_agent(
        ctx.obj["socket"],
        journal_dir=journal_dir,
        artifacts_dir=artifacts_dir,
        allowed_uid=allowed_uid,
    )
