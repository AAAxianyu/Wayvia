"""Probe evidence must represent the current protocol state, not arbitrary text."""

import json
import socket
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from wayvia.adapters.base import CommandResult
from wayvia.agent import probe_runtime
from wayvia.agent.models import Probe
from wayvia.agent.probes import verify


@pytest.fixture
def management_path():
    # macOS pytest temp roots can exceed sockaddr_un's 104-byte path limit.
    with tempfile.TemporaryDirectory(prefix="wv-management-", dir="/tmp") as directory:
        yield Path(directory) / "management.sock"


@pytest.mark.parametrize(
    "output",
    [
        {"healthy": False, "status": "healthy", "layer": "access"},
        {"healthy": "true", "status": "healthy"},
        ["healthy"],
        None,
    ],
)
def test_helper_result_cannot_inject_healthy_evidence(tmp_path, output):
    executor = SimpleNamespace(
        config_dir=tmp_path, _cmd=lambda *a, **k: CommandResult(0, json.dumps(output))
    )
    evidence = verify(executor, [Probe(layer="vpn", kind="vpn_session", target="10.77.0.1")])
    assert evidence[0]["status"] == "failed"
    assert evidence[0]["layer"] == "vpn"
    assert evidence[0]["kind"] == "vpn_session"


@pytest.mark.parametrize(
    ("state_response", "status_response", "healthy"),
    [
        ("1720000002,CONNECTED,SUCCESS,10.77.0.2,203.0.113.1,1194", "", True),
        ("1720000002,RECONNECTING,connection-reset,,,,", "", False),
        (
            "1720000001,CONNECTED,SUCCESS,10.77.0.2,203.0.113.1,1194\n"
            "1720000002,RECONNECTING,connection-reset,,,,",
            "",
            False,
        ),
        (
            "1720000002,CONNECTED,SUCCESS,10.77.0.2,203.0.113.1,1194\n"
            "1720000002,RECONNECTING,connection-reset,,,,",
            "",
            False,
        ),
        (
            "1720000002,RECONNECTING,connection-reset,,,,\nEND\n"
            "1720000003,CONNECTED,SUCCESS,10.77.0.2,203.0.113.1,1194",
            "",
            False,
        ),
        (
            "1720000002,RECONNECTING,connection-reset,,,,",
            "CLIENT_LIST\tpeer,CONNECTED,SUCCESS,old-session\t203.0.113.1\t10.77.0.2",
            False,
        ),
        ("not-a-timestamp,CONNECTED,SUCCESS,,,,", "", False),
        ("", "CLIENT_LIST\tpeer,CONNECTED,SUCCESS,\t203.0.113.1\t10.77.0.2", False),
    ],
)
def test_management_observation_uses_current_state_response(
    management_path, monkeypatch, state_response, status_response, healthy
):
    path = management_path
    received = []
    failures = []
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path))
        listener.listen(1)
        listener.settimeout(2)

        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(2)
                    connection.sendall(b">INFO:OpenVPN Management Interface Version 5\n")
                    for expected, response in (
                        (b"state 1\n", f"{state_response}\nEND\n"),
                        (b"status 3\n", f"{status_response}\nEND\n"),
                        (b"quit\n", ""),
                    ):
                        request = b""
                        while not request.endswith(b"\n"):
                            chunk = connection.recv(4096)
                            if not chunk:
                                raise OSError("incomplete command")
                            request += chunk
                        received.append(request)
                        assert request == expected
                        if response:
                            connection.sendall(response.encode())
            except Exception as exc:
                failures.append(exc)

        class ManagementSocket(socket.socket):
            def connect(self, address):
                assert address == "/run/wayvia/vpn-client.sock"
                return super().connect(str(path))

        monkeypatch.setattr(
            probe_runtime,
            "socket",
            SimpleNamespace(socket=ManagementSocket, AF_UNIX=socket.AF_UNIX),
        )
        thread = threading.Thread(target=serve)
        thread.start()
        try:
            result = probe_runtime.vpn_session({"timeout": 1}, {"role": "ingress"})
        finally:
            thread.join(timeout=3)
        assert not thread.is_alive()
    assert not failures
    assert received == [b"state 1\n", b"status 3\n", b"quit\n"]
    assert result["healthy"] is healthy
