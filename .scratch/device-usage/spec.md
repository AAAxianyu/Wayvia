# Independent devices and usage

User authorization: revoke existing subscriptions/shared account; create independent Android and desktop accounts/subscriptions; show online connections, recent proxy activity and upload/download totals; deliver desktop Mihomo link. Do not contact providers or run fault/reboot tests.

Use sing-box 1.14.3 authenticated loopback native API, fixed read-only agent operation, periodic least-privilege collector, durable per-connection high-water marks. No source/target addresses, payloads or secrets in telemetry. Online means observed active authenticated proxy connections, not physical device reachability. Stale/failed collection is unknown. Totals count proxied payload bytes since collection started; domestic direct traffic and transport overhead are excluded; gaps are explicit and totals are not billing-grade. Subscription fetch is tracked separately from use.

Production baseline 20e33ab; do not include unrelated candidate stability or client-adapter branches. Three old subscription IDs revoked before deployment, shared credential will be replaced in one sync. Existing 24-hour observation continues; credential maintenance restart must be annotated separately.
