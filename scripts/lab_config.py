"""Generate laboratory artifacts through the production config and profile renderers."""

import json
import secrets
from pathlib import Path

from wayvia.config import WayviaConfig, example_config
from wayvia.render import ProfileRenderer, ServerRenderer


def generate(directory: Path, *, laboratory: bool) -> None:
    data = example_config()
    if laboratory:
        data["deployment"]["mode"] = "lab"
        data["policy"]["domestic_domain_rules_ref"] = None
        data["entry"].update(endpoint="10.72.0.1:443", server_name="entry.lab", listen="10.72.0.1")
        data["carrier"].update(
            endpoint="carrier.lab:443", server_name="carrier.lab", listen="10.73.0.2"
        )
        data["vpn"].update(subnet="10.77.0.0/24", server_name="vpn-server.lab")
        data["rulesets"] = {
            "domestic-rules": {
                "source": "controlled laboratory classification",
                "version": "lab-1",
                "domains": ["domestic.lab"],
                "cidrs": ["10.71.0.0/24", "fd72:1::/64"],
            },
            "local-rules": {
                "source": "loopback only for controlled topology",
                "version": "lab-1",
                "domains": ["localhost"],
                "cidrs": ["127.0.0.0/8", "::1/128"],
            },
        }
        for name in ["domestic-dns", "internal-dns"]:
            data["dns"]["resolvers"][name]["address"] = "10.71.0.1"
        data["dns"]["resolvers"]["exit-dns"]["address"] = "10.74.0.2"
        data["dns"]["resolvers"]["bootstrap-dns"]["address"] = "10.73.0.1"
    config = WayviaConfig.model_validate(data)
    password = secrets.token_urlsafe(32)
    carrier = secrets.token_urlsafe(32)
    device = {
        "id": "lab-device",
        "name": "Disposable lab device",
        "platform": "android",
        "status": "active",
    }
    credentials = {"lab-device": password, "carrier-credential": carrier}
    directory.mkdir(parents=True, exist_ok=True)
    for role in ["ingress", "exit"]:
        target = directory / role
        target.mkdir()
        for name, content in ServerRenderer(config).render(role, [device], credentials).items():
            (target / name).write_text(content)
    for format, extension in [("sing-box", "json"), ("mihomo", "yaml")]:
        (directory / ("profile-" + format + "." + extension)).write_text(
            ProfileRenderer(config).render(device, password, format)
        )
    (directory / "lab-credentials.json").write_text(
        json.dumps({"device": password, "carrier": carrier})
    )
    (directory / "logical-config.json").write_text(config.model_dump_json(indent=2))
    for path in directory.rglob("*"):
        if path.is_file():
            path.chmod(0o600)
