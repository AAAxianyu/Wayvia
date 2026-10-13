import json
import os
import platform
import pwd
from types import SimpleNamespace

from typer.testing import CliRunner

from wayvia.cli import app


def test_cli_persistent_control_plane_and_fail_closed_default(tmp_path):
    runner = CliRunner()
    prefix = ["--data-dir", str(tmp_path), "--mode", "demo"]
    result = runner.invoke(app, prefix + ["init"])
    assert result.exit_code == 0, result.output
    assert runner.invoke(app, ["validate", str(tmp_path / "config.yaml")]).exit_code == 0
    result = runner.invoke(app, prefix + ["policy", "explain", "baidu.com"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["action"] == "direct"
    result = runner.invoke(app, prefix + ["device", "add", "phone"])
    assert result.exit_code == 0, result.output
    device = json.loads(result.stdout)
    assert device["status"] == "active" and device["simulated"]
    result = runner.invoke(
        app, prefix + ["profile", "export", device["id"], "--format", "sing-box"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["inbounds"][0]["type"] == "tun"
    result = runner.invoke(app, prefix + ["subscription", "issue", device["id"]])
    assert result.exit_code == 0, result.output
    sub = json.loads(result.stdout)
    assert runner.invoke(app, prefix + ["subscription", "revoke", sub["id"]]).exit_code == 0
    production = ["--data-dir", str(tmp_path)]
    result = runner.invoke(app, production + ["device", "add", "pending"])
    assert result.exit_code == 0, result.output
    pending = json.loads(result.stdout)
    assert pending["status"] == "pending" and not pending["simulated"]
    result = runner.invoke(app, production + ["profile", "export", pending["id"]])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "device_inactive"


def test_cli_invalid_config_and_existing_init_are_nonzero(tmp_path):
    runner = CliRunner()
    prefix = ["--data-dir", str(tmp_path)]
    assert runner.invoke(app, prefix + ["init"]).exit_code == 0
    assert runner.invoke(app, prefix + ["init"]).exit_code == 2
    (tmp_path / "invalid.yaml").write_text("schema_version: 99\nsecret: never-echo-this\n")
    result = runner.invoke(app, ["validate", str(tmp_path / "invalid.yaml")])
    assert result.exit_code == 2
    assert "never-echo-this" not in result.output
    assert json.loads(result.stdout)["code"] == "invalid_config"


def test_version_without_subcommand():
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.0"


def test_system_mode_uses_the_same_control_state_and_identity_as_api(monkeypatch):
    import wayvia.cli as module

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=1234))
    monkeypatch.setattr(os, "geteuid", lambda: 1234)
    observed = {}

    def service(ctx):
        observed.update(ctx.obj)
        return SimpleNamespace(publication=lambda: {"status": "unpublished"})

    monkeypatch.setattr(module, "service", service)
    result = CliRunner().invoke(app, ["--system", "config", "publication"])
    assert result.exit_code == 0, result.output
    assert str(observed["root"]) == "/var/lib/wayvia/control"
    assert str(observed["config"]) == "/var/lib/wayvia/control/config.yaml"
    assert str(observed["socket"]) == "/run/wayvia/agent.sock"
    assert observed["mode"] == "production"


def test_system_mode_rejects_different_identity_and_split_state_options(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=1234))
    monkeypatch.setattr(os, "geteuid", lambda: 5678)
    result = CliRunner().invoke(app, ["--system", "config", "publication"])
    assert result.exit_code == 3
    assert json.loads(result.stdout)["code"] == "system_identity_required"
    result = CliRunner().invoke(app, ["--system", "--data-dir", str(tmp_path), "status"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "system_options_conflict"


def test_render_and_plan_accept_documented_role_and_output_options(monkeypatch, tmp_path):
    import wayvia.agent.planner as planner
    import wayvia.cli as module
    import wayvia.config as config_module

    monkeypatch.setattr(module, "artifacts", lambda ctx, config, role: {"manifest.json": "{}"})
    monkeypatch.setattr(config_module, "load_config", lambda path: SimpleNamespace(revision="r1"))
    monkeypatch.setattr(
        planner,
        "build_plan",
        lambda **kwargs: SimpleNamespace(digest="plan-id", model_dump_json=lambda **ignored: "{}"),
    )
    rendered = tmp_path / "rendered"
    result = CliRunner().invoke(
        app, ["render", "input.yaml", "--role", "ingress", "--output", str(rendered)]
    )
    assert result.exit_code == 0, result.output
    assert (rendered / "manifest.json").read_text() == "{}"
    plan = tmp_path / "plan.json"
    result = CliRunner().invoke(
        app,
        [
            "plan",
            "input.yaml",
            "--role",
            "ingress",
            "--output-file",
            str(plan),
            "--baseline",
            "explicit",
        ],
    )
    assert result.exit_code == 0, result.output
    assert plan.read_text() == "{}"
