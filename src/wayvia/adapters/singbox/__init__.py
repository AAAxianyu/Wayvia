"""Real sing-box validation and lifecycle via the owned systemd unit."""

import re
from pathlib import Path

from wayvia.adapters.base import AdapterError, CommandRunner


class SingBoxAdapter:
    simulated = False

    def __init__(self, runner=None, binary="sing-box", expected_version="1.14.3"):
        self.runner = runner or CommandRunner()
        self.binary = binary
        self.expected_version = expected_version

    def inspect(self):
        output = self.runner.run([self.binary, "version"]).stdout
        match = re.search(r"sing-box version (\S+)", output)
        if not match:
            raise AdapterError("VERSION_UNKNOWN", "Cannot identify sing-box version")
        return {
            "component": "sing-box",
            "version": match[1],
            "supported": match[1] == self.expected_version,
        }

    def validate(self, config_path: Path):
        version = self.inspect()
        if not version["supported"]:
            raise AdapterError(
                "VERSION_MISMATCH", "sing-box version is not locked", details=version
            )
        self.runner.run([self.binary, "check", "-c", str(config_path)])
        return {**version, "validated": True}

    def stop(self, unit="wayvia-ingress.service"):
        if unit not in {
            "wayvia-ingress.service",
            "wayvia-bridge.service",
            "wayvia-carrier.service",
            "wayvia-exit.service",
        }:
            raise AdapterError("RESOURCE_FORBIDDEN", "Unit is outside Wayvia ownership")
        self.runner.run(["systemctl", "stop", unit])
