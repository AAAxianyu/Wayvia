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
from wayvia.errors import ExitCode, WayviaError

app = typer.Typer(no_args_is_help=True, help="Wayvia split-routing gateway control plane")
device_app = typer.Typer(no_args_is_help=True)
subscription_app = typer.Typer(no_args_is_help=True)
policy_app = typer.Typer(no_args_is_help=True)
profile_app = typer.Typer(no_args_is_help=True)
secret_app = typer.Typer(no_args_is_help=True)
rules_app = typer.Typer(no_args_is_help=True)
app.add_typer(device_app, name="device")
app.add_typer(subscription_app, name="subscription")
app.add_typer(policy_app, name="policy")
app.add_typer(profile_app, name="profile")
app.add_typer(secret_app, name="secret")
app.add_typer(rules_app, name="rules")


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


@app.callback(invoke_without_command=True)
def options(
    ctx: typer.Context,
    data_dir: Path = Path(".wayvia"),
    config: Path | None = None,
    mode: str = "production",
    agent_socket: Path = Path("/run/wayvia/agent.sock"),
    version: bool = typer.Option(False, "--version", is_eager=True),
):
    if version:
        typer.echo(__version__)
        raise typer.Exit()
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
def issue(ctx: typer.Context, device: str, format: str = "sing-box", lifetime: int = 2592000):
    output(service(ctx).issue(device, format, lifetime))


@subscription_app.command("revoke")
@guarded
def revoke_subscription(ctx: typer.Context, subscription: str):
    output(service(ctx).revoke_subscription(subscription))


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
def render(ctx: typer.Context, config: Path, role: str, output: Path):
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
    role: str,
    output_file: Path,
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
