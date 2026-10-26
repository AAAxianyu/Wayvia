"""Read-only cross-role timelines and allowlisted, checksummed private bundles."""

import hashlib
import io
import json
import math
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from wayvia.evidence import token
from wayvia.observation_run import private
from wayvia.telemetry.records import SOURCES, EvidenceError, canonical, project, stamp, timestamp
from wayvia.telemetry.store import read_database

MAX_REPORT_BYTES = 16 * 1024 * 1024


def export_json(report, output):
    """Exclusive owner-private JSON export, also used by usage and alert reports."""
    output = Path(output).absolute()
    if any(p.is_symlink() for p in (output, *output.parents)):
        raise EvidenceError("EVIDENCE_UNSAFE_PATH")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private(output.parent, directory=True)
    data = canonical(report) + b"\n"
    if len(data) > MAX_REPORT_BYTES:
        raise EvidenceError("EVIDENCE_EXPORT_TOO_LARGE")
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {"status": "exported", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def coverage(samples, *, start, end, interval, now):
    slots = max(1, math.ceil((end - start) / interval))
    observed = {
        min(slots - 1, int((timestamp(s["observed_at"]) - start) // interval)) for s in samples
    }
    missing = sorted(set(range(slots)) - observed)
    ranges = []
    for slot in missing:
        if ranges and ranges[-1][1] == slot:
            ranges[-1][1] = slot + 1
        else:
            ranges.append([slot, slot + 1])
    status = "pending" if now < end else ("complete" if not missing else "incomplete")
    return {
        "status": status,
        "status_scope": "sampling_cadence; per_source_completeness_is_reported_separately",
        "interval_seconds": interval,
        "expected_slots": slots,
        "observed_slots": len(observed),
        "missing_slots": len(missing),
        "fraction": len(observed) / slots,
        "missing_ranges": [
            {"start": stamp(start + a * interval), "end": stamp(min(end, start + b * interval))}
            for a, b in ranges
        ],
        "source_ok_slots": {
            source: len(
                {
                    min(slots - 1, int((timestamp(s["observed_at"]) - start) // interval))
                    for s in samples
                    if s.get("sources", {}).get(source) == "ok"
                }
            )
            for source in SOURCES
        },
        "clock_alignment": "unknown"
        if any(s.get("clock_offset_ms") is None for s in samples) or not samples
        else "measured",
        "collector_max_duration_ms": max(
            (s.get("collector", {}).get("duration_ms") or 0 for s in samples), default=None
        ),
        "collector_max_cpu_ms": max(
            (s.get("collector", {}).get("cpu_ms") or 0 for s in samples), default=None
        ),
        "collector_max_rss_bytes": max(
            (s.get("collector", {}).get("rss_bytes") or 0 for s in samples), default=None
        ),
    }


def timeline(
    paths,
    *,
    start=None,
    end=None,
    incident_id=None,
    timezone="UTC",
    device_id=None,
    layer=None,
    role=None,
    now=None,
):
    now = datetime.now(UTC).timestamp() if now is None else now
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise EvidenceError("EVIDENCE_TIMEZONE_INVALID") from None
    if device_id is not None and token(device_id, r"[a-f0-9]{32}") is None:
        raise EvidenceError("EVIDENCE_DEVICE_INVALID")
    if role is not None and role not in {"ingress", "exit"}:
        raise EvidenceError("EVIDENCE_ROLE_INVALID")
    if layer is not None and layer not in {
        "access",
        "carrier",
        "vpn",
        "egress",
        "dns",
        "ipv6",
        "return",
    }:
        raise EvidenceError("EVIDENCE_LAYER_INVALID")
    if incident_id is not None and token(incident_id, r"[a-f0-9]{32}") is None:
        raise EvidenceError("EVIDENCE_INCIDENT_INVALID")
    if not 1 <= len(paths) <= 8:
        raise EvidenceError("EVIDENCE_SOURCE_LIMIT")
    records, sources, incidents = {}, [], []
    # Read each source in a snapshot. No schema migration, checkpoint or pruning.
    for path in paths:
        with read_database(path) as (connection, metadata):
            incident = (
                connection.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
                if incident_id
                else None
            )
            sources.append(
                {
                    "metadata": metadata,
                    "incident": dict(incident) if incident else None,
                    "path": path,
                }
            )
            if incident:
                incidents.append(dict(incident))
    if incident_id and not incidents:
        raise EvidenceError("EVIDENCE_INCIDENT_MISSING")
    if start is None:
        start = min(i["start"] for i in incidents) if incidents else None
    else:
        start = timestamp(start) if isinstance(start, str) else start
    if end is None:
        end = max(i["end"] for i in incidents) if incidents else None
    else:
        end = timestamp(end) if isinstance(end, str) else end
    if start is None or end is None or not start < end or end - start > 86400:
        raise EvidenceError("EVIDENCE_WINDOW_INVALID")
    path_ids = {source["metadata"].get("path_id") for source in sources}
    if len(path_ids) != 1:
        raise EvidenceError("EVIDENCE_PATH_MISMATCH")
    source_meta = []
    total_read_bytes = 0
    for source in sources:
        metadata = source["metadata"]
        with read_database(source["path"]) as (connection, current):
            if metadata != current:
                raise EvidenceError("EVIDENCE_SOURCE_CHANGED")
            rows = connection.execute(
                "SELECT data FROM records WHERE at>=? AND at<=? ORDER BY at,id LIMIT 20001",
                (start, end),
            )
            for index, row in enumerate(rows):
                total_read_bytes += len(row[0].encode())
                if index >= 20000 or total_read_bytes > MAX_REPORT_BYTES:
                    raise EvidenceError("EVIDENCE_QUERY_LIMIT")
                raw = json.loads(row[0])
                clean = project(
                    raw, client_authorized=raw.get("source") in {"client", "user_confirmed"}
                )
                ident = clean["event_id"]
                if ident in records and canonical(records[ident]) != canonical(clean):
                    raise EvidenceError("EVIDENCE_ID_CONFLICT")
                records[ident] = clean
        source_meta.append(
            {
                key: metadata.get(key)
                for key in ("role", "run_id", "path_id", "revision", "generation")
            }
            | {
                "incident_pinned": source["incident"] is not None,
                "retention_seconds": metadata["policy"]["retention_seconds"],
                "interval_seconds": metadata["policy"]["interval_seconds"],
            }
        )
    all_rows = sorted(records.values(), key=lambda row: (row["observed_at"], row["event_id"]))
    all_samples = [r for r in all_rows if r["type"] == "sample"]
    coverage_by_role = {}
    for wanted_role in ("ingress", "exit"):
        role_sources = [s for s in source_meta if s["role"] == wanted_role]
        interval = max((s["interval_seconds"] for s in role_sources), default=5)
        coverage_by_role[wanted_role] = coverage(
            [s for s in all_samples if s["role"] == wanted_role],
            start=start,
            end=end,
            interval=interval,
            now=now,
        )
        coverage_by_role[wanted_role]["source_available"] = bool(role_sources)
        coverage_by_role[wanted_role]["incident_pinned"] = any(
            s["incident_pinned"] for s in role_sources
        )
    selected = [
        r
        for r in all_rows
        if (role is None or r["role"] == role)
        and (device_id is None or r.get("device_id") == device_id)
        and (layer is None or r.get("layer") == layer)
    ]
    for row in selected:
        row["observed_at_local"] = (
            datetime.fromisoformat(row["observed_at"]).astimezone(zone).isoformat()
        )
    result = {
        "schema_version": 1,
        "generated_at": stamp(now),
        "timezone": timezone,
        "window": {"start": stamp(start), "end": stamp(end)},
        "incident_id": incident_id,
        "filters": {"device_id": device_id, "layer": layer, "role": role},
        "sources": source_meta,
        "coverage": coverage_by_role,
        "events": [r for r in selected if r["type"] == "event"],
        "samples": [r for r in selected if r["type"] == "sample"],
        "limits": {
            "business_impact": "unknown_without_authorized_client_evidence",
            "cross_role_correlation": "explicit_incident_id; time_proximity_is_not_causality",
            "device_identity": "credential_id; consult_identity_source",
            "coverage_scope": "window_samples; device_filter_does_not_attribute_shared_path",
        },
    }
    if len(canonical(result)) > MAX_REPORT_BYTES:
        raise EvidenceError("EVIDENCE_EXPORT_TOO_LARGE")
    return result


def export_bundle(report, output, *, retain_seconds=604800):
    if not 600 <= retain_seconds <= 2592000:
        raise EvidenceError("EVIDENCE_RETENTION_INVALID")
    output = Path(output).absolute()
    if any(p.is_symlink() for p in (output, *output.parents)):
        raise EvidenceError("EVIDENCE_UNSAFE_PATH")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private(output.parent, directory=True)
    payloads = {
        "events.jsonl": b"".join(canonical(row) + b"\n" for row in report["events"]),
        "samples.jsonl": b"".join(canonical(row) + b"\n" for row in report["samples"]),
        "coverage.json": canonical(report["coverage"]),
    }
    manifest = {
        key: value for key, value in report.items() if key not in {"events", "samples", "coverage"}
    }
    manifest.update(
        expires_at=stamp(timestamp(report["generated_at"]) + retain_seconds),
        access="owner_only_0600",
        retention_seconds=retain_seconds,
        sha256={key: hashlib.sha256(value).hexdigest() for key, value in payloads.items()},
    )
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in {"manifest.json": canonical(manifest), **payloads}.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, value)
    if len(data.getvalue()) > MAX_REPORT_BYTES:
        raise EvidenceError("EVIDENCE_EXPORT_TOO_LARGE")
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data.getvalue())
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {
        "status": "exported",
        "sha256": hashlib.sha256(data.getvalue()).hexdigest(),
        "expires_at": manifest["expires_at"],
        "bytes": len(data.getvalue()),
    }


def verify_bundle(path, *, now=None):
    path = Path(path)
    private(path)
    now = datetime.now(UTC).timestamp() if now is None else now
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise EvidenceError("EVIDENCE_EXPORT_TOO_LARGE")
    try:
        with zipfile.ZipFile(path) as archive:
            expected = {"manifest.json", "events.jsonl", "samples.jsonl", "coverage.json"}
            if (
                set(archive.namelist()) != expected
                or len(archive.namelist()) != 4
                or sum(i.file_size for i in archive.infolist()) > MAX_REPORT_BYTES
            ):
                raise EvidenceError("EVIDENCE_BUNDLE_INVALID")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or not isinstance(manifest.get("sha256"), dict):
                raise EvidenceError("EVIDENCE_BUNDLE_INVALID")
            if manifest.get("schema_version") != 1 or timestamp(manifest.get("expires_at")) <= now:
                raise EvidenceError("EVIDENCE_BUNDLE_EXPIRED")
            for name in expected - {"manifest.json"}:
                raw = archive.read(name)
                if hashlib.sha256(raw).hexdigest() != manifest["sha256"].get(name):
                    raise EvidenceError("EVIDENCE_CHECKSUM_MISMATCH")
            return {
                "status": "verified",
                "expires_at": manifest["expires_at"],
                "integrity": "checksum_not_remote_authentication",
            }
    except (zipfile.BadZipFile, ValueError, KeyError, RuntimeError):
        raise EvidenceError("EVIDENCE_BUNDLE_INVALID") from None
