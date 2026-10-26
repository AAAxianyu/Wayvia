"""One client contract for Verge/Mihomo; no operating-system profile forks."""

import re
import unicodedata
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from wayvia.errors import WayviaError

GROUP = "Wayvia"
NODE = "Wayvia · 主线路"
TEST_URL = "https://www.gstatic.com/generate_204"
UPDATE_HOURS = 24


def title(device_name):
    value = "".join(c for c in device_name if not unicodedata.category(c).startswith("C"))
    value = re.sub(r'[\\/<>:"|?*]', " ", value)
    return "Wayvia · " + (" ".join(value.split())[:60] or "个人订阅")


def headers(device_name, format):
    suffix = "yaml" if format == "mihomo" else "json"
    filename = quote(title(device_name) + "." + suffix, safe="")
    return {
        "Content-Disposition": (
            f"attachment; filename=\"Wayvia.{suffix}\"; filename*=UTF-8''{filename}"
        ),
        "profile-update-interval": str(UPDATE_HOURS),
        "X-Wayvia-Profile-Contract": "1",
        "Referrer-Policy": "no-referrer",
    }


def https_url(value):
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.fragment
            or any(ord(c) < 33 or ord(c) == 127 for c in value)
            or url.port is not None
            and not 1 <= url.port <= 65535
        ):
            raise ValueError()
        return url
    except (TypeError, ValueError):
        raise WayviaError(
            "CLIENT_URL_INVALID", "A valid HTTPS URL without embedded credentials is required"
        ) from None


def import_link(url):
    parsed = https_url(url)
    if not re.fullmatch(
        r"/(?:[A-Za-z0-9_.~-]+/)*subscriptions/[A-Za-z0-9_-]{64}/mihomo", parsed.path
    ):
        raise WayviaError(
            "CLIENT_SUBSCRIPTION_URL_INVALID", "Expected a Wayvia Mihomo subscription URL"
        )
    # Encode the whole credential-bearing URL exactly once; never request it here.
    return "clash://install-config?" + urlencode({"url": url}, quote_via=quote)


def subscription_url(base_url, path):
    base = https_url(base_url)
    if (
        base.query
        or base.path not in {"", "/"}
        or not re.fullmatch(r"/subscriptions/[A-Za-z0-9_-]{64}/mihomo", path)
    ):
        raise WayviaError(
            "CLIENT_BASE_URL_INVALID", "Use the HTTPS subscription origin without a path or query"
        )
    return urlunsplit((base.scheme, base.netloc, path, "", ""))


def contract():
    return {
        "schema_version": 1,
        "client_family": "clash-verge-rev_mihomo",
        "profile_format": "mihomo",
        "platform_settings_required": False,
        "supported_platforms": ["windows", "macos", "linux"],
        "gui_acceptance": "separate_from_core_configuration_validation",
        "group": GROUP,
        "node": NODE,
        "path_count": 1,
        "delay": {
            "url": TEST_URL,
            "timeout_ms": 5000,
            "periodic_interval_seconds": 0,
            "unit": "ms",
            "scope": "HTTPS_over_selected_node_not_bandwidth_or_business_SLA",
        },
        "routing": {
            "domestic": "direct",
            "protected": "Wayvia_fail_closed",
            "foreign_ipv6": "reject",
            "client_stopped": "OS_dependent_not_persistent_kill_switch",
        },
        "update_hours": UPDATE_HOURS,
        "traffic_scope": "best_effort_proxy_payload_not_billing",
        "quota_bytes": None,
        "service_expires_at": None,
    }
