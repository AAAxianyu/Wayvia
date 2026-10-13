#!/usr/bin/env python3
"""Nonprivileged audit reproductions. Uses temporary files only; no host networking.

Run from repository root with .venv/bin/python docs/validation/review-2026-10-12/control/reproduce.py.
Optional first argument: repository root. Optional second argument: output directory.
All dataplane acceptance in fixture setup is explicitly simulated; publication
reproduction then switches to the real production ControlService/AgentDataplane
with an absent socket and demonstrates that export never consults it.
"""
import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

repo = Path(sys.argv[1] if len(sys.argv) > 1 else Path.cwd()).resolve()
output_dir = Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).parent).resolve()
sys.path.insert(0, str(repo / "src"))

import yaml
from typer.testing import CliRunner
from wayvia.cli import app
from wayvia.config import WayviaConfig, example_config
from wayvia.policy import PolicyCompiler
from wayvia.render import ProfileRenderer, ServerRenderer
from wayvia.services.control import AgentDataplane, ControlService, SimulatedDataplane

output_dir.mkdir(parents=True, exist_ok=True)
source = example_config()
source["rulesets"]["domestic-domains"] = {
    "source": "explicit audit fixture", "version": "audit-1", "domains": ["cn"]
}
base = WayviaConfig.model_validate(source)
results = {}

# Case 1: precedence mismatch on accepted overlapping local/domestic rules.
dns_data = deepcopy(source)
dns_data["rulesets"]["local-rules"]["domains"].append("campus.example")
dns_data["rulesets"]["domestic-domains"]["domains"] = ["campus.example"]
dns_config = WayviaConfig.model_validate(dns_data)
compiled = PolicyCompiler(dns_config).explain(domain="sso.campus.example")
profile = ProfileRenderer(dns_config)._mihomo("explicit-nonsecret-audit-fixture")
actual_dns = profile["dns"]["nameserver-policy"]["+.campus.example"]
expected_dns = ["192.168.1.1:53#DIRECT"]
results["dns_priority"] = {
    "reproduced": compiled["dns"] == "internal" and actual_dns != expected_dns,
    "input": {"domain": "sso.campus.example", "local_suffix": "campus.example", "domestic_suffix": "campus.example"},
    "config_validation": "accepted",
    "policy_explainer_dns": compiled["dns"],
    "expected_mihomo_dns": expected_dns,
    "actual_mihomo_dns": actual_dns,
    "evidence_kind": "real configuration compiler and renderer; no networking",
    "source": "src/wayvia/render/__init__.py:236-243",
}

# Case 2: crash exactly after real secret write/fsync and before the DB update.
class InjectedCrash(BaseException):
    pass

with TemporaryDirectory(prefix="wayvia-audit-rotation-") as tmp:
    root = Path(tmp)
    control = ControlService(root, base, mode="test", dataplane=SimulatedDataplane())
    device = control.add("audit", "android")
    original_write = control.secrets.write
    def crash_after_write(ref, value):
        original_write(ref, value)
        raise InjectedCrash()
    control.secrets.write = crash_after_write
    try:
        control.rotate(device["id"])
    except InjectedCrash:
        pass
    restarted = ControlService(root, base, mode="test", dataplane=SimulatedDataplane())
    restarted.reconcile()
    try:
        restarted.rotate(device["id"])
        retry = "success"
    except Exception as error:
        retry = type(error).__name__
    state = restarted.device(device["id"])
    results["rotation_crash"] = {
        "reproduced": retry == "FileExistsError" and state["credential_version"] == 1,
        "failure_point": "after SecretStore.write fsync returns, before devices UPDATE",
        "credential_version": state["credential_version"],
        "post_restart_reconcile_status": state["status"],
        "retry_result": retry,
        "orphan_v2_present": (restarted.secrets.root / ("device-" + device["id"] + "-v2")).exists(),
        "evidence_kind": "real SQLite and SecretStore; explicitly simulated dataplane; injected process interruption",
        "source": "src/wayvia/services/control.py:223-231; src/wayvia/secrets/__init__.py:54-61",
    }

# Case 3: actual production export code path publishes a new un-applied revision.
# Initial active DB row represents an already accepted old credential; setup is
# simulated. The server artifact is a real renderer snapshot for the old config,
# NOT a claim that a live service was started in this nonprivileged reproduction.
with TemporaryDirectory(prefix="wayvia-audit-revision-") as tmp:
    root = Path(tmp)
    setup = ControlService(root, base, mode="test", dataplane=SimulatedDataplane())
    device = setup.add("audit", "android")
    old_files = ServerRenderer(base).render(
        "ingress", [{"id": device["id"], "password": "explicit-device-fixture-password"}],
        {base.carrier.credential_ref: "explicit-carrier-fixture-password"},
    )
    previous_artifact = json.loads(old_files["ingress.json"])
    changed = deepcopy(source)
    changed["entry"]["endpoint"] = "unapplied-new-entry.example:8443"
    changed["entry"]["server_name"] = "unapplied-new-entry.example"
    new_config = WayviaConfig.model_validate(changed)
    real_dataplane = AgentDataplane(root / "absent-agent.sock")
    production = ControlService(root, new_config, mode="production", dataplane=real_dataplane)
    exported = json.loads(production.export(device["id"], "sing-box"))
    published = exported["outbounds"][0]
    # Also prove that normal production subscription issue + fetch lacks the gate.
    subscription = production.issue(device["id"], "sing-box")
    fetched = json.loads(production.fetch(subscription["token"], "sing-box"))
    results["publication_revision"] = {
        "reproduced": published["server_port"] == 8443 and previous_artifact["inbounds"][0]["listen_port"] == 443,
        "scenario": "existing active credential/database and old accepted config; operator loads new entry settings before or after a failed new network apply",
        "old_entry": base.entry.endpoint,
        "old_configuration_revision": base.revision,
        "old_server_artifact_listen_port": previous_artifact["inbounds"][0]["listen_port"],
        "new_configuration_revision": new_config.revision,
        "new_config_apply_called": False,
        "production_mode": production.mode,
        "production_dataplane_type": type(production.dataplane).__name__,
        "agent_socket_exists": real_dataplane.socket_path.exists(),
        "device_status": production.device(device["id"])["status"],
        "published_entry": published["server"],
        "published_port": published["server_port"],
        "subscription_issue_and_fetch_succeeded": fetched["outbounds"][0]["server_port"] == 8443,
        "evidence_kind": "real production export/issue/fetch + real SQLite; absent real agent; initial active row from explicit simulated fixture; old artifact is rendered snapshot, not live server",
        "limit": "No real deployment or traffic was started. This proves the software publication gate is absent; operational impact follows when the live server still uses the old confirmed configuration.",
        "source": "src/wayvia/services/control.py:259-306; src/wayvia/adapters/linux/__init__.py:532-555",
    }

# Case 4: existing convention returns a status object and exit 0 on pending/degraded.
with TemporaryDirectory(prefix="wayvia-audit-cli-") as tmp:
    root = Path(tmp)
    (root / "config.yaml").write_text(yaml.safe_dump(source))
    runner = CliRunner()
    args = ["--data-dir", str(root), "--agent-socket", str(root / "absent.sock")]
    add_response = runner.invoke(app, args + ["device", "add", "audit-phone"])
    added = json.loads(add_response.stdout)
    revoke_response = runner.invoke(app, args + ["device", "revoke", added["id"]])
    revoked = json.loads(revoke_response.stdout)
    results["cli_status_exit_convention"] = {
        "reproduced": add_response.exit_code == 0 and revoke_response.exit_code == 0 and revoked["status"] == "degraded",
        "classification": "interface contract tradeoff; caller must inspect returned status; not classified as a must-fix in the parent audit",
        "add_exit": add_response.exit_code,
        "add_state": added["status"],
        "revoke_exit": revoke_response.exit_code,
        "revoke_state": revoked["status"],
        "protection": revoked["evidence"]["protection"],
        "evidence_kind": "real CLI production mode + absent Unix agent socket; temporary SQLite; no host network mutation",
        "source": "src/wayvia/cli.py:164-185",
    }

report = {
    "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
    "repository": str(repo),
    "scope": "Read-only product-source audit with disposable local metadata; no product source modifications, no host network changes, no ICT access",
    "all_expected_reproductions_observed": all(case["reproduced"] for case in results.values()),
    "cases": results,
}
for name, value in results.items():
    (output_dir / (name + ".json")).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
(output_dir / "results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
print(json.dumps(report, indent=2, ensure_ascii=False))
sys.exit(0 if report["all_expected_reproductions_observed"] else 1)
