"""Render reviewable safe access logging; never edits a live configuration."""

import re

PREAMBLE = r"""
map $uri $wayvia_log_route {
    default unknown_path;
    ~^/subscriptions/[^/]+/info$ subscription_info;
    ~^/subscriptions/[^/]+/[^/]+$ subscription_download;
    /healthz health;
    ~^/(admin|api)(/|$) management_denied;
}
map $uri $wayvia_log_format {
    default unknown;
    ~^/subscriptions/[^/]+/mihomo$ mihomo;
    ~^/subscriptions/[^/]+/sing-box$ sing-box;
}
map $request_method $wayvia_log_method {
    default OTHER;
    GET GET; HEAD HEAD; POST POST; PUT PUT; PATCH PATCH;
    DELETE DELETE; OPTIONS OPTIONS;
}
log_format wayvia_safe escape=json
    '{"wayvia_log":1,"request_id":"$request_id","at":"$msec",'
    '"ip":"$remote_addr","method":"$wayvia_log_method",'
    '"route":"$wayvia_log_route","format":"$wayvia_log_format",'
    '"status":"$status","bytes":"$body_bytes_sent",'
    '"duration":"$request_time","upstream":"$upstream_response_time",'
    '"rate":"$limit_req_status","concurrent":"$limit_conn_status"}';
"""


def render(original, socket_path="/run/wayvia-log/events.sock"):
    if not re.fullmatch(r"/[A-Za-z0-9_./-]{1,95}", socket_path) or ".." in socket_path:
        raise ValueError("Unsafe log socket")
    if (
        "wayvia_safe" in original
        or original.count("access_log off;") != 1
        or original.count("server {") != 1
    ):
        raise ValueError("Unsupported nginx baseline")
    if "error_log /dev/null;" not in original:
        raise ValueError("Raw subscription error logging must remain disabled")
    anchor = "location ^~ /subscriptions/ {"
    if original.count(anchor) != 1:
        raise ValueError("Subscription location missing")
    location = original.split(anchor, 1)[1].split("\n    }", 1)[0]
    if "proxy_set_header Host " not in location:
        raise ValueError("Explicit proxy headers required in subscription location")
    access = f"access_log syslog:server=unix:{socket_path},nohostname,tag=wayvia wayvia_safe;"
    body = original.replace("access_log off;", access, 1)
    body = body.replace(
        anchor, anchor + "\n        proxy_set_header X-Wayvia-Request-ID $request_id;", 1
    )
    # At server level preserve the baseline's existing proxy_set_header inheritance.
    return PREAMBLE + "\n" + body
