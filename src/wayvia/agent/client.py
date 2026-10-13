"""Only local Unix-domain sockets are accepted by the privileged client."""

import json
import socket

from wayvia.adapters.base import AdapterError

MAX_MESSAGE = 2 * 1024 * 1024


class AgentClient:
    def __init__(self, socket_path="/run/wayvia/agent.sock", timeout=90):
        self.socket_path, self.timeout = str(socket_path), timeout

    def request(self, payload):
        data = json.dumps(payload).encode() + b"\n"
        if len(data) > MAX_MESSAGE:
            raise AdapterError("REQUEST_TOO_LARGE", "Agent message exceeds its bounded size")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.socket_path)
                connection.sendall(data)
                response = b""
                while not response.endswith(b"\n"):
                    block = connection.recv(65536)
                    if not block:
                        raise AdapterError(
                            "AGENT_PROTOCOL", "Agent closed without a complete response"
                        )
                    response += block
                    if len(response) > MAX_MESSAGE:
                        raise AdapterError(
                            "AGENT_PROTOCOL", "Agent response exceeds its size limit"
                        )
            result = json.loads(response)
        except (OSError, ValueError) as exc:
            raise AdapterError("AGENT_UNAVAILABLE", "Local agent is unavailable") from exc
        if not result.get("ok"):
            error = result.get("error", {})
            raise AdapterError(
                error.get("code", "AGENT_FAILED"),
                error.get("message", "Agent request failed"),
                details=error.get("details", {}),
            )
        return result["result"]
