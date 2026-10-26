"""Read-only chrony System time projection. No time adjustment or remote command."""

import re
from datetime import UTC, datetime


def tracking(raw, now):
    try:
        fields = dict(line.split(":", 1) for line in raw.splitlines() if ":" in line)
        fields = {k.strip(): v.strip() for k, v in fields.items()}
        if (
            fields["Leap status"] != "Normal"
            or not 1 <= int(fields["Stratum"]) <= 15
            or fields["Reference ID"].split()[0] in {"7F7F0101", "00000000"}
        ):
            return {}
        match = re.fullmatch(
            r"([0-9]+(?:\.[0-9]+)?) seconds (fast|slow) of NTP time", fields["System time"]
        )
        if not match:
            return {}
        offset = float(match[1]) * (1 if match[2] == "fast" else -1)
        reference = datetime.strptime(fields["Ref time (UTC)"], "%a %b %d %H:%M:%S %Y").replace(
            tzinfo=UTC
        )
        if not 0 <= now - offset - reference.timestamp() <= 300 or abs(offset) > 86400:
            return {}
        return {
            "clock_sync": "synchronized",
            "clock_offset_ms": offset * 1000,
            "clock_offset_source": "chrony_tracking",
            "clock_offset_observed_at": datetime.fromtimestamp(now, UTC).isoformat(),
        }
    except (ValueError, KeyError, OverflowError, IndexError):
        return {}
