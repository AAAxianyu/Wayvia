"""Nginx limit insertion must preserve unrelated server and privacy settings."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "subscription_limits", Path(__file__).parents[2] / "deploy/subscription_limits.py"
)
limits = importlib.util.module_from_spec(spec)
spec.loader.exec_module(limits)

BASE = b"""server {
    listen 9443 ssl;
    access_log off;
    location = /wayvia-probe { return 200; }
    location ^~ /subscriptions/ {
        limit_except GET { deny all; }
        proxy_pass http://127.0.0.1:8787;
    }
    location / { return 404; }
}
"""


def test_render_adds_only_http_zones_and_scoped_limits():
    actual = limits.render(BASE)
    assert actual.startswith(limits.ZONES)
    assert actual.replace(limits.ZONES, b"", 1).replace(limits.LIMITS, b"", 1) == BASE
    assert b"rate=5r/m" in actual and b"burst=5 nodelay" in actual
    assert b"limit_conn wayvia_sub_concurrent 2;" in actual
    assert b"limit_req_status 429;" in actual and b"limit_conn_status 429;" in actual


@pytest.mark.parametrize(
    "bad",
    [BASE.replace(limits.ANCHOR, b""), BASE + BASE, limits.ZONES + BASE, b"server {}\n"],
)
def test_render_rejects_drift_and_already_installed(bad):
    with pytest.raises(ValueError):
        limits.render(bad)


def test_apply_restores_original_when_reload_fails(tmp_path, monkeypatch):
    config = tmp_path / "wayvia.conf"
    config.write_bytes(BASE)
    monkeypatch.setattr(limits, "CONFIG", config)
    monkeypatch.setattr(limits, "BACKUPS", tmp_path / "backups")
    monkeypatch.setattr(
        limits.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b""),
    )
    calls = []

    def reload_once_fails():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("synthetic reload failure")

    monkeypatch.setattr(limits, "check_and_reload", reload_once_fails)
    with pytest.raises(RuntimeError, match="synthetic reload failure"):
        limits.apply(limits.sha(BASE), limits.sha(limits.render(BASE)))
    assert config.read_bytes() == BASE
    assert len(calls) == 2
    assert list((tmp_path / "backups").glob("*/original.conf"))[0].read_bytes() == BASE


def test_restore_refuses_drift_and_restores_verified_candidate(tmp_path, monkeypatch):
    config = tmp_path / "wayvia.conf"
    config.write_bytes(BASE)
    monkeypatch.setattr(limits, "CONFIG", config)
    monkeypatch.setattr(limits, "BACKUPS", tmp_path / "backups")
    monkeypatch.setattr(
        limits.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b""),
    )
    monkeypatch.setattr(limits, "check_and_reload", lambda: None)
    result = limits.apply(limits.sha(BASE), limits.sha(limits.render(BASE)))
    candidate = config.read_bytes()
    config.write_bytes(candidate + b"# concurrent change\n")
    with pytest.raises(ValueError, match="changed"):
        limits.restore(result["backup"])
    assert config.read_bytes().endswith(b"# concurrent change\n")
    config.write_bytes(candidate)
    assert limits.restore(result["backup"])["status"] == "restored"
    assert config.read_bytes() == BASE
