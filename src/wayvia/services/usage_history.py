"""Sample-end payload deltas with explicit uncertainty; never reconstruct missing minutes."""

import json
import math
import re
import time

from wayvia.errors import WayviaError

INTERVAL = 5
MINUTE_RETENTION = 30 * 86400
HOUR_RETENTION = 365 * 86400
REASONS = {
    "baseline",
    "core_generation_changed",
    "collection_gap",
    "counter_reset",
    "closed_history_overflow",
    "connection_final_counter_missing",
    "cross_bucket_interval",
}


def client_claim(value):
    # Header is untrusted. Never store the original string, OS, model, URL or user name.
    match = re.fullmatch(
        r"(sing-box|mihomo|Clash|ClashMeta|ClashForAndroid|Stash|Shadowrocket|clash-verge)/v?"
        r"([0-9]{1,3}(?:\.[0-9]{1,4}){0,3})(?:-(?:rc|alpha|beta)(?:[.-]?[0-9]{1,4})?)?",
        value or "",
    )
    return match.groups() if match else ("unknown", "unknown")


def update(
    connection,
    device,
    now,
    *,
    up=0,
    down=0,
    unallocated_up=0,
    unallocated_down=0,
    covered=0,
    count=None,
    used=None,
    reasons=(),
    sample=False,
    fetch=False,
):
    for grain in (60, 3600):
        bucket = int(now // grain) * grain
        connection.execute(
            "INSERT OR IGNORE INTO usage_history(device_id,grain,bucket) VALUES(?,?,?)",
            (device, grain, bucket),
        )
        row = connection.execute(
            "SELECT * FROM usage_history WHERE device_id=? AND grain=? AND bucket=?",
            (device, grain, bucket),
        ).fetchone()
        flags = set(json.loads(row["uncertainty"])) | (set(reasons) & REASONS)
        if covered and now - covered < bucket:
            flags.add("cross_bucket_interval")
        connection.execute(
            "UPDATE usage_history SET upload_bytes=upload_bytes+?,download_bytes=download_bytes+?,"
            "unallocated_upload_bytes=unallocated_upload_bytes+?,"
            "unallocated_download_bytes=unallocated_download_bytes+?,samples=samples+?,"
            "connections_peak=?,connections_last=?,last_used_at=?,first_observed_at=?,"
            "last_observed_at=?,subscription_fetches=subscription_fetches+?,uncertainty=? "
            "WHERE device_id=? AND grain=? AND bucket=?",
            (
                up,
                down,
                unallocated_up,
                unallocated_down,
                int(sample),
                max(count, row["connections_peak"] or 0)
                if count is not None
                else row["connections_peak"],
                count if count is not None else row["connections_last"],
                max(used or 0, row["last_used_at"] or 0) or None,
                row["first_observed_at"] or (now if sample else None),
                now if sample else row["last_observed_at"],
                int(fetch),
                json.dumps(sorted(flags)),
                device,
                grain,
                bucket,
            ),
        )
        # Coverage is bounded consecutive sampled time, split at bucket boundaries.
        # This is cadence coverage, not proof every connection's final counter survived.
        cursor = now - covered
        while cursor < now:
            part = int(cursor // grain) * grain
            end = min(now, part + grain)
            connection.execute(
                "INSERT OR IGNORE INTO usage_history(device_id,grain,bucket) VALUES(?,?,?)",
                (device, grain, part),
            )
            connection.execute(
                "UPDATE usage_history SET covered_seconds=covered_seconds+? "
                "WHERE device_id=? AND grain=? AND bucket=?",
                (end - cursor, device, grain, part),
            )
            cursor = end


def fetched(connection, device, now, declaration):
    family, version = client_claim(declaration)
    bucket = int(now // 60) * 60
    update(connection, device, now, fetch=True)
    connection.execute(
        "INSERT INTO usage_fetch_history VALUES(?,?,?,?,1,?) ON CONFLICT(device_id,bucket,client) "
        "DO UPDATE SET fetches=fetches+1,last_at=excluded.last_at,version=excluded.version",
        (device, bucket, family, version, now),
    )
    prune(connection, now)


def prune(connection, now):
    # Hour rows are accumulated transactionally with minute rows; retries cannot double-roll-up.
    for grain, retention in ((60, MINUTE_RETENTION), (3600, HOUR_RETENTION)):
        connection.execute(
            "DELETE FROM usage_history WHERE grain=? AND bucket<?",
            (grain, int((now - retention) // grain) * grain),
        )
    connection.execute(
        "DELETE FROM usage_fetch_history WHERE bucket<?",
        (int((now - MINUTE_RETENTION) // 60) * 60,),
    )


def history(db, device, *, start, end, grain="minute", now=None):
    now = time.time() if now is None else now
    if (
        grain not in {"minute", "hour"}
        or type(start) not in {int, float}
        or type(end) not in {int, float}
        or not math.isfinite(start)
        or not math.isfinite(end)
        or not 0 <= start < end <= now + 60
    ):
        raise WayviaError("USAGE_WINDOW_INVALID", "Invalid usage history window")
    step = 60 if grain == "minute" else 3600
    first, stop = int(start // step) * step, math.ceil(end / step) * step
    retention = MINUTE_RETENTION if step == 60 else HOUR_RETENTION
    cutoff = int((now - retention) // step) * step
    if (stop - first) // step > 10000:
        raise WayviaError("USAGE_QUERY_LIMIT", "Select a shorter window or hourly history")
    with db.connect() as connection:
        if not connection.execute("SELECT 1 FROM devices WHERE id=?", (device,)).fetchone():
            raise WayviaError("device_not_found", "Device not found")
        rows = {
            r["bucket"]: dict(r)
            for r in connection.execute(
                (
                    "SELECT * FROM usage_history WHERE device_id=? AND grain=? AND "
                    "bucket>=? AND bucket<?"
                ),
                (device, step, max(first, cutoff), stop),
            )
        }
        claims = [
            dict(r)
            for r in connection.execute(
                "SELECT bucket,client,version,fetches,last_at FROM usage_fetch_history "
                "WHERE device_id=? AND bucket>=? AND bucket<? "
                "ORDER BY bucket,client,version LIMIT 10001",
                (device, max(first, int((now - MINUTE_RETENTION) // 60) * 60), stop),
            )
        ]
        if len(claims) > 10000:
            raise WayviaError(
                "USAGE_QUERY_LIMIT", "Select a shorter window for client declarations"
            )
    result = []
    for at in range(first, stop, step):
        row = rows.get(at)
        if row is None:
            result.append(
                {
                    "bucket": at,
                    "status": "expired" if at < cutoff else "unknown",
                    "upload_bytes": None,
                    "download_bytes": None,
                    "active_connections": None,
                    "coverage_fraction": 0,
                }
            )
            continue
        row.pop("device_id")
        row.pop("grain")
        row["uncertainty"] = json.loads(row["uncertainty"])
        row["status"] = "sampled" if row["samples"] else "unobserved"
        if not row["samples"]:
            row["upload_bytes"] = row["download_bytes"] = None
        row["coverage_fraction"] = min(1, row["covered_seconds"] / step)
        row["partial_query_bucket"] = at < start or at + step > end
        row["observation_delay_seconds"] = (
            max(0, now - row["last_observed_at"]) if row["last_observed_at"] else None
        )
        result.append(row)
    return {
        "schema_version": 1,
        "device_id": device,
        "generated_at": now,
        "requested_window": {"start": start, "end": end},
        "bucket_window": {"start": first, "end": stop},
        "grain_seconds": step,
        "interval_seconds": INTERVAL,
        "minute_retention_seconds": MINUTE_RETENTION,
        "hour_retention_seconds": HOUR_RETENTION,
        "buckets": result,
        "subscription_client_claims": claims,
        "limits": {
            "accounting": "best_effort_proxy_payload_not_billing",
            "byte_attribution": "sample_end_bucket; uncertain_bytes_separate_not_timed",
            "connections": "credential_activity_not_physical_reachability",
            "client": "unverified_allowlisted_declaration; detail_retained_30_days",
            "history": "no_retrospective_backfill; missing_is_unknown",
        },
    }
