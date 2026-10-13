# Wayvia development

Read `docs/design.md`, `docs/development.md`, and `docs/development-goal.md` before changes. Preserve domestic direct routing, VPN-over-proxy order, server-only carrier secrets, and fail-closed protected traffic.

Work on `codex/` branches. Never change development-host routes, global firewalls, existing VPNs, or unspecified production servers. Network experiments run only in explicitly disposable Linux containers/VMs.

Run `make check`, `make demo`, `make test-configs`, and `make test-lab`. A missing experiment environment is a nonzero, explicit result. Record versions and evidence in `compatibility.yaml`, `docs/implementation-status.md`, and `docs/validation/`; distinguish software, isolated paths, and real deployment acceptance.
