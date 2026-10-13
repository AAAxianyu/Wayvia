"""OpenVPN has no TLS configuration dry-run: validate in an isolated namespace."""

import re
import shlex
from pathlib import Path

from wayvia.adapters.base import AdapterError, CommandRunner

# Deliberately exclude arbitrary scripts, plugins, nested configs and output paths.
SAFE_OPTIONS = set(
    "client server dev dev-type proto remote local port socks-proxy topology "
    "ifconfig ifconfig-pool-persist ca cert key dh tls-crypt tls-auth key-direction "
    "remote-cert-tls verify-x509-name tls-version-min data-ciphers auth verb mute "
    "persist-key persist-tun nobind resolv-retry route route-nopull route-noexec "
    "ifconfig-noexec redirect-gateway keepalive ping ping-restart explicit-exit-notify "
    "push disable-dco auth-nocache allow-compression tun-mtu mssfix reneg-sec "
    "tls-server tls-client server-ipv6 route-ipv6 script-security cipher "
    "connect-retry connect-timeout verify-client-cert".split()
)


def check_safe_config(text):
    inline = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if inline:
            if line == f"</{inline}>":
                inline = None
            continue
        if line in {"<ca>", "<cert>", "<key>", "<tls-crypt>", "<dh>"}:
            inline = line[1:-1]
            continue
        try:
            words = shlex.split(line)
        except ValueError as exc:
            raise AdapterError("VPN_CONFIG_INVALID", "Invalid OpenVPN option quoting") from exc
        if not words or words[0] not in SAFE_OPTIONS:
            raise AdapterError(
                "VPN_OPTION_FORBIDDEN", "OpenVPN option is outside the fixed execution contract"
            )
        if words[0] == "script-security" and words[1:] != ["1"]:
            raise AdapterError("VPN_OPTION_FORBIDDEN", "User scripts are forbidden")
        if words[0] in {"ca", "cert", "key", "tls-crypt", "tls-auth", "dh"}:
            if (
                len(words) != 2
                or (words[1] != "none" and not words[1].startswith("/etc/wayvia/secrets/"))
                or ".." in words[1]
            ):
                raise AdapterError(
                    "SECRET_PATH_FORBIDDEN", "VPN material must be in Wayvia secrets"
                )
    if inline:
        raise AdapterError("VPN_CONFIG_INVALID", "Unclosed inline certificate")


class OpenVPNAdapter:
    simulated = False

    def __init__(self, runner=None, binary="openvpn", expected_version="2.6.19"):
        self.runner = runner or CommandRunner()
        self.binary = binary
        self.expected_version = expected_version

    def inspect(self):
        output = self.runner.run([self.binary, "--version"]).stdout
        match = re.search(r"OpenVPN (\d+\.\d+(?:\.\d+)?)", output)
        if not match:
            raise AdapterError("VERSION_UNKNOWN", "Cannot identify OpenVPN version")
        return {
            "component": "openvpn",
            "version": match[1],
            "supported": match[1] == self.expected_version,
        }

    def validate(self, config_path: Path, namespace=None):
        check_safe_config(config_path.read_text())
        version = self.inspect()
        if not version["supported"]:
            raise AdapterError(
                "VERSION_MISMATCH", "OpenVPN version is not supported", details=version
            )
        if namespace != "wayvia-validation":
            raise AdapterError(
                "ISOLATION_REQUIRED",
                "TLS validation requires the explicit isolated wayvia-validation namespace",
            )
        # --test-crypto ignores TLS; never present that as TLS configuration validation.
        # dev null + route-noexec prevent interface/route mutation; the isolated local
        # SOCKS/socket endpoint receives only the intended validation handshake.
        result = self.runner.observe(
            [
                "ip",
                "netns",
                "exec",
                namespace,
                self.binary,
                "--config",
                str(config_path),
                "--dev",
                "null",
                "--route-noexec",
                "--ifconfig-noexec",
                "--script-security",
                "0",
                "--connect-retry-max",
                "1",
                "--connect-timeout",
                "2",
            ],
            timeout=8,
            marker="Initialization Sequence Completed",
        )
        if (
            result.returncode
            or "Initialization Sequence Completed" not in result.stdout + result.stderr
        ):
            raise AdapterError(
                "VPN_VALIDATION_FAILED",
                "Isolated OpenVPN handshake did not complete",
                details={"returncode": result.returncode},
            )
        return {**version, "validated": True, "scope": "isolated-handshake"}
