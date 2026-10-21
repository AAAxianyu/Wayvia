"""Read-only modes, identities and access checks; no credential contents are read."""

import json
import pathlib
import pwd
import subprocess

user = pwd.getpwnam("wayvia")
paths = [
    "/opt/wayvia",
    "/opt/wayvia/.venv",
    "/opt/wayvia/.venv/lib/python3.12/site-packages/wayvia",
    "/etc/wayvia",
    "/etc/wayvia/secrets",
    "/etc/wayvia/agent.env",
    "/var/lib/wayvia",
    "/var/lib/wayvia/control",
    "/var/lib/wayvia/control/config.yaml",
    "/var/lib/wayvia/control/secrets",
]


def readable(identity, path):
    return (
        subprocess.run(
            ["runuser", "-u", identity, "--", "test", "-r", str(path)], capture_output=True
        ).returncode
        == 0
    )


result = {
    "modes": {p: oct(pathlib.Path(p).stat().st_mode & 0o777) for p in paths},
    "service_identity_CLI": subprocess.run(
        ["runuser", "-u", "wayvia", "--", "/opt/wayvia/.venv/bin/wayvia", "--version"],
        capture_output=True,
    ).returncode
    == 0,
}
keys = list(pathlib.Path("/etc/wayvia/secrets").glob("*.key"))
result["server_private_keys"] = {
    "count": len(keys),
    "all_root_0600": all(p.stat().st_uid == 0 and p.stat().st_mode & 0o777 == 0o600 for p in keys),
    "none_readable_by_API": all(not readable("wayvia", p) for p in keys),
}
secrets = [p for p in pathlib.Path("/var/lib/wayvia/control/secrets").rglob("*") if p.is_file()]
result["control_secrets"] = {
    "count": len(secrets),
    "all_service_uid_0600": all(
        p.stat().st_uid == user.pw_uid and p.stat().st_mode & 0o777 == 0o600 for p in secrets
    ),
    "none_readable_by_nobody": all(not readable("nobody", p) for p in secrets),
}
print(json.dumps(result, indent=2))
