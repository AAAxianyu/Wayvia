import json

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
