# Independent device accounts and usage — 2026-10-21

Branch: `codex/device-usage-20261021`, baseline `20e33ab`. This branch deliberately excludes the unmerged TLS-probe fallback candidate and subscription-client adapter branch. The user's primary workspace is unchanged. Fault injection, failover and reboot tests remain deferred.

## Delivered behavior

- Existing three subscription tokens revoked; the shared `android-primary` account subsequently revoked as well, closing its sessions. A 401 on subscription download alone does **not** invalidate imported credentials. The old account is absent from the ingress and durable recovery credentials.
- New `phone-primary` (Android) and `computer-primary` (desktop) use independent random credentials and separate Mihomo subscriptions. Subscription lifetime is one year. Private delivery material remains outside Git, with owner-only permissions.
- Device list/status API and `device usage` expose active connections, last proxy use, upload/download/total bytes, observation freshness and coverage gaps. `subscription list` exposes metadata and download timestamps/counts without tokens. Public subscription routes cannot access those administrator endpoints.
- Five-second collector, loopback authenticated sing-box API, no externally published management interface. Counter data contains no source/target addresses, business content or credentials.

## Commands

Run on the Ali ingress over the existing authorized SSH channel:

```sh
sudo -u wayvia /opt/wayvia/.venv/bin/wayvia --system device usage
sudo -u wayvia /opt/wayvia/.venv/bin/wayvia --system subscription list
systemctl status wayvia-usage.service
```

`online` means at least one observed authenticated proxy connection; an idle client with no connection is offline for this metric. Failed/stale collection is unknown. Last use is separate from subscription fetching. Totals count only proxy payload since collection began, not domestic traffic, network encapsulation or earlier shared-account history. They are best-effort totals with explicit gaps; sharing a device link combines its usage. See [ADR-007](../../adr/007-device-usage-observations.md).

## Verification

- `make check`: 377 passed, three existing deprecation warnings; format, lint, build and CLI checks passed.
- `make demo`: passed, explicitly simulated.
- `make test-configs`: 10 existing real-component checks passed.
- Additional disposable Linux container normal-path test: sing-box 1.14.3 accepts the native API configuration; unauthenticated reads are rejected; each of two credentials has correctly attributed live and closed connections, 78 upload bytes / 65,651 download bytes. See `native-api-normal.json`. The first harness attempt used a fixed 250 ms delay and missed connection establishment; it was replaced with a bounded readiness wait. No production fault was involved.
- `make test-lab`: **not run**, deferred by the user because it contains fault/recovery tests. No current full-lab pass claimed.
- Production deployment and account revocation evidence are recorded separately below/in companion JSON. Validation requests themselves contribute to account totals and are not evidence that a physical phone or computer is in use.

## Maintenance and continuing stability observation

Old shared-account revocation and ingress reload: **2026-10-21 10:23:23.601774–10:23:25.806653 UTC**. Afterward, the old credential returned curl 35 / HTTP 000 while the valid internal control returned HTTP 200 against the same HTTPS target, at 10:25:07.862468 UTC. This was an authorized account change, not a naturally occurring disconnect.

The separate 24-hour `wayvia-observe-stability-20261020` sampler is not restarted or extended. Credential changes and the package/collector deployment window must be excluded from natural-fault attribution. The deployment temporarily suspends the normal monitor timer while replacing package files, resumes it afterward, and leaves the independent observer running. Japan is not changed.


## Production acceptance

Package and account maintenance: **2026-10-21 10:36:45.579584–10:37:16.225518 UTC**. Wheel SHA-256: `7d312506bc5ec20def75bded8bd909fd3b8627ab95376ab550bb2c66e7e8d7a2`. Both new accounts returned HTTPS 200; active connections and positive upload/download were attributed independently. A post-deployment old-account test again returned HTTP 000, with the internal control returning HTTP 200. Original routing and TLS configuration compare equal after excluding users and the added local API.

Both new subscriptions returned HTTP 200 through the public IP HTTPS endpoint with certificate verification enabled. They contain the same routing policy and different passwords; only the Ali entry is included. Public `/api/v1/devices` remains HTTP 404. The new desktop YAML passed **Mihomo Meta v1.19.13 Linux arm64** `-t` validation in a network-isolated disposable container. This is configuration/core validation, not a new physical desktop import acceptance.

At 10:40:55 UTC the two accounts had no active proxy connections. Each had about 6 KB from these server-side validation requests, with no observed accounting gaps. The collector was running with zero service restarts; the independent stability observer remained active. Audit evidence: `deployment.json`, `acceptance.json`, `old-account-test.json`, `subscription-https.json`, `final-state.json`. A maintenance marker is also saved privately alongside the server's existing observation samples.

## Next steps / limitations

- Import the new desktop URL; the previous profile no longer authenticates. The new phone URL is separately prepared and must be imported when that device is available.
- Physical client acceptance of the new links is pending; automated validation traffic is explicitly identified above.
- Continue the already scheduled stability window, treating the two maintenance windows separately from natural incidents. No new attribution to Japan or cross-border transport is made here.
- If precise billing-grade statistics or hardware binding is later needed, this best-effort credential-level observer is not sufficient. Current counters and freshness are suitable for checking which account has proxy activity.
- Root-only backup and delivery files remain under `/root/wayvia-device-usage-20261021`; local delivery material is ignored and mode 0600. Metadata migration 3 adds usage and fetch fields. Never restore the pre-revocation backup or an old device-state snapshot to recover telemetry; doing so could reactivate retired credentials. The upgrade backup was taken **after** the shared credential was revoked.
