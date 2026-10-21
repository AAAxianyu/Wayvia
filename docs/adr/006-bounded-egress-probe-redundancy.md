# 006: Bounded egress identity probes and TCP latency

Status: accepted, 2026-10-20

An observed exit-side HTTP timeout to the ingress host caused DEGRADED and stopped
the carrier, followed by ingress recovery that rebuilt the chain. The original
network timeout preceded the stops; rebuilding amplified the outage. Probing an
exit by returning across the same international link makes exit readiness depend
on an unnecessary round trip and a single HTTP endpoint.

HTTP probes now optionally accept `response_format: cloudflare_trace` and up to
two `fallback_urls`. Primary and alternatives require HTTPS, no credentials or
fragments. A trace must contain exactly one valid `ip=` equal to the expected
exit address. A fresh matching response is still required; nothing succeeds from
cached evidence or a TCP connection alone. Ingress access and namespace egress
must declare identical endpoint sets, format and expected identity.

Alternatives share the existing timeout, reserving a portion for each remaining
endpoint. Only transport/timeouts and HTTP 429/5xx may try another endpoint;
identity mismatches, TLS failures, redirects and malformed/oversized responses
fail immediately. Curl does not follow redirects, use ambient proxies or load
curlrc. Responses are capped at 16 KiB. Evidence records per-endpoint index,
duration, curl status and HTTP status, without response bodies, credentials or
URLs. The outer helper deadline kills its process group as before. Legacy text
probes and sealed v1 plan digests retain their meaning when new options default.

Deployment uses the TLS-verified IP URLs `https://1.1.1.1/cdn-cgi/trace` and
`https://1.0.0.1/cdn-cgi/trace`, avoiding namespace stub DNS and the Japan-to-China
return dependency. Both belong to Cloudflare: this is endpoint redundancy, not
independent-provider availability. Failure of all endpoints still blocks the
path. Existing explicit safety blocks, namespace firewall, monitor state machine
and full recovery rules remain unchanged. No grace-period success is introduced.

Both rendered OpenVPN TCP endpoints set `socket-flags TCP_NODELAY`, the only
allowed socket flag. The authenticated probe relay also disables Nagle. This
keeps VPN-over-proxy and certificate authentication intact. It addresses small
packet coalescing; it does not eliminate TCP retransmission head-of-line blocking
or guarantee bandwidth improvement. Measure production effects separately.

References: [OpenVPN 2.6 manual](https://openvpn.net/community-docs/community-articles/openvpn-2-6-manual.html),
[Cloudflare /cdn-cgi endpoints](https://developers.cloudflare.com/fundamentals/reference/cdn-cgi-endpoint/).

### Completed-response teardown correction (2026-10-20 follow-up)

A read-only production reproduction captured curl exit 0, HTTP 200 and the correct
fresh exit identity, followed by `ConnectionResetError` from relay teardown. The
context manager exception replaced the completed result and could exhaust both
alternatives despite successful responses. Keep a completed curl result when
teardown raises `ConnectionError`, then apply every existing curl TLS/status,
identity and overall deadline check. A pre-response reset, incomplete request,
TLS exception or expired deadline still fails. Evidence marks
`transport_closed_after_response`; it never fabricates a response or uses a cache.

This fixes a proven false-negative mechanism. Historical transport-only failures
did not preserve their curl results, so they cannot all be attributed conclusively
to this mechanism. Simultaneous HTTP/DNS timeouts remain unresolved. See the
[server follow-up](../validation/server-followup-2026-10-20/README.md).
