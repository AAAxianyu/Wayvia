"""Independent read-only evidence evaluator and explicit, durable notification outbox.

Evaluation never calls a dataplane, collector, scheduler, or delivery channel.
Unknown evidence cannot resolve health alerts. No raw log text reaches notifications.
"""

import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from wayvia import __version__
from wayvia.evidence import producer_digest, token
from wayvia.observation_run import ObservationError, private, read_private
from wayvia.telemetry.records import (
    EvidenceError,
    Policy,
    canonical,
    digest,
    project,
    stamp,
    timestamp,
)
from wayvia.telemetry.report import coverage, export_json
from wayvia.telemetry.store import read_database, secure_path

ID = Annotated[str, Field(pattern=r"^[a-zA-Z0-9-]{1,64}$")]


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["ingress", "exit"]
    database: Path
    run_directory: Path


class Watch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: ID
    path_id: ID
    sources: Annotated[list[Source], Field(min_length=1, max_length=2)]
    usage_database: Path | None = None
    required_sources: list[
        Literal["tcp", "interfaces", "host", "clock", "journal", "audit", "peer"]
    ] = ["tcp", "interfaces", "host", "clock", "journal"]
    window_seconds: Annotated[int, Field(ge=60, le=3600)] = 300
    stale_seconds: Annotated[int, Field(ge=10, le=300)] = 20
    evidence_fresh_seconds: Annotated[int, Field(ge=30, le=3600)] = 120
    min_coverage: Annotated[float, Field(ge=0, le=1)] = 0.8
    min_free_bytes: Annotated[int, Field(ge=0)] = 64 * 1024 * 1024
    require_clock_offset: bool = False
    max_clock_offset_ms: Annotated[float, Field(gt=0, le=60000)] = 100
    max_offset_spread_ms: Annotated[float, Field(gt=0, le=60000)] = 250

    @model_validator(mode="after")
    def both_roles(self):
        if {s.role for s in self.sources} != {"ingress", "exit"}:
            raise ValueError("Configure both expected roles, including unavailable sources")
        return self


class Channel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["directory", "https"]
    destination: str
    authorization_file: Path | None = None

    @model_validator(mode="after")
    def endpoint(self):
        if self.kind == "https":
            value = urlsplit(self.destination)
            if (
                value.scheme != "https"
                or not value.hostname
                or value.username
                or value.password
                or value.query
                or value.fragment
            ):
                raise ValueError("HTTPS URL must not contain credentials, query or fragment")
        elif not Path(self.destination).is_absolute() or self.authorization_file:
            raise ValueError("Directory channel requires an absolute owner-private path")
        return self


class AlertConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    watches: Annotated[list[Watch], Field(min_length=1, max_length=16)]
    channel: Channel | None = None

    @model_validator(mode="after")
    def unique(self):
        if len({w.id for w in self.watches}) != len(self.watches):
            raise ValueError("Duplicate watch IDs")
        return self


def load_config(path):
    return AlertConfig.model_validate(read_private(secure_path(path)))


def inspect_source(source, watch, now, *, disk_usage=shutil.disk_usage):
    """Only declared private run/store paths; source failure is an integrity finding."""
    result = {
        "role": source.role,
        "active": True,
        "causes": [],
        "events": [],
        "incidents": [],
        "coverage": None,
        "offset_ms": None,
        "uncertainty": [],
        "reference": None,
    }
    try:
        directory = source.run_directory.absolute()
        if any(p.is_symlink() for p in (directory, *directory.parents)):
            raise EvidenceError()
        private(directory, directory=True)
        manifest = read_private(directory / "run.json")
        contract = manifest["contract"]
        if (
            manifest.get("schema_version") != 1
            or token(manifest.get("run_id"), r"[a-zA-Z0-9-]{1,64}") is None
            or token(contract.get("revision"), r"[a-f0-9]{64}") is None
            or token(contract.get("generation"), r"[a-f0-9]{32}") is None
        ):
            raise EvidenceError()
        result["reference"] = {
            "run_id": manifest["run_id"],
            "role": source.role,
            "revision": contract["revision"],
            "generation": contract["generation"],
        }
        if (
            contract["role"] != source.role
            or contract["path_id"] != watch.path_id
            or contract.get("mode") != "rolling_passive"
        ):
            raise EvidenceError()
        created, until = timestamp(manifest["created_at"]), timestamp(contract["until"])
        if created > now + 5 or not created < until or until - created > 86400:
            raise EvidenceError()
        stopped = directory / "stop.json"
        if stopped.exists() or stopped.is_symlink():
            if read_private(stopped).get("reason") != "user_stop":
                raise EvidenceError()
            result.update(active=False)
            return result
        if now >= until:
            result.update(active=False)
            return result
        state = read_private(directory / "state.json")
        if state.get("status") != "running":
            result["causes"].append("collector_unexpected_stop")
            if state.get("reason") == "storage_unavailable":
                result["causes"].append("evidence_persistence_failed")
        with read_database(source.database) as (connection, metadata):
            if (
                any(
                    metadata.get(k) != contract.get(k)
                    for k in ("role", "revision", "generation", "path_id")
                )
                or metadata["run_id"] != manifest["run_id"]
            ):
                raise EvidenceError()
            result["reference"] = {
                k: metadata[k] for k in ("role", "revision", "generation", "run_id", "path_id")
            }
            policy = Policy.model_validate(metadata["policy"])
            interval = policy.interval_seconds
            start = max(created, now - watch.window_seconds)
            rows = connection.execute(
                "SELECT data FROM records WHERE at>=? AND at<=? ORDER BY at,id LIMIT 20001",
                (start, now),
            ).fetchall()
            if len(rows) > 20000 or sum(len(r[0]) for r in rows) > 16 * 1024 * 1024:
                raise EvidenceError()
            clean = [project(json.loads(r[0]), client_authorized=True) for r in rows]
            # Membership in a pinned time window alone is not cross-role causality.
            triggers = dict(
                connection.execute(
                    "SELECT t.record,t.incident FROM triggers t JOIN records r ON r.id=t.record "
                    "WHERE r.at>=? AND r.at<=? LIMIT 20001",
                    (start, now),
                )
            )
            for row in clean:
                if (
                    row["type"] == "event"
                    and row["role"] == source.role
                    and row["revision"] == metadata["revision"]
                    and row["generation"] == metadata["generation"]
                ):
                    row["incident_id"] = triggers.get(row["event_id"])
                    result["events"].append(row)
            result["incidents"] = [
                dict(r)
                for r in connection.execute(
                    "SELECT id,expires FROM incidents ORDER BY trigger_at DESC LIMIT 128"
                )
            ]
            samples = [
                r
                for r in clean
                if r["type"] == "sample"
                and r["role"] == source.role
                and r["revision"] == metadata["revision"]
                and r["generation"] == metadata["generation"]
            ]
            # Ignore the incomplete current slot; cold-start time is explicit.
            end = now - interval
            if end > start:
                result["coverage"] = coverage(
                    [s for s in samples if timestamp(s["observed_at"]) <= end],
                    start=start,
                    end=end,
                    interval=interval,
                    now=now,
                )
            latest = samples[-1] if samples else None
            if latest is None or now - timestamp(latest["observed_at"]) > watch.stale_seconds:
                if now - created > watch.stale_seconds:
                    result["causes"].append("collector_heartbeat_stale")
            if result["coverage"] and now - created >= 60:
                cov = result["coverage"]
                cov["missing_ranges_total"] = len(cov["missing_ranges"])
                if len(cov["missing_ranges"]) > 32:
                    result["uncertainty"].append("missing_ranges_truncated_to_32")
                    cov["missing_ranges"] = cov["missing_ranges"][:32]
                if cov["fraction"] < watch.min_coverage:
                    result["causes"].append("sample_coverage_low")
                for name in watch.required_sources:
                    if cov["source_ok_slots"][name] / cov["expected_slots"] < watch.min_coverage:
                        result["causes"].append("source_gap_" + name)
            probes = [
                r
                for r in result["events"]
                if r["kind"]
                in {
                    "probe_healthy",
                    "probe_failed",
                    "health_degraded",
                    "health_healthy",
                    "recovery_completed",
                }
            ]
            if (
                not probes
                or now - timestamp(probes[-1]["observed_at"]) > watch.evidence_fresh_seconds
            ):
                if now - created > watch.evidence_fresh_seconds:
                    result["causes"].append("health_evidence_expired")
            if latest:
                if latest["clock_sync"] == "unsynchronized":
                    result["causes"].append("clock_unsynchronized")
                result["offset_ms"] = (
                    latest["clock_offset_ms"]
                    if now - timestamp(latest["observed_at"]) <= watch.stale_seconds
                    else None
                )
            if result["offset_ms"] is None:
                result["uncertainty"].append("clock_offset_unknown")
                if watch.require_clock_offset:
                    result["causes"].append("clock_offset_unavailable")
            elif abs(result["offset_ms"]) > watch.max_clock_offset_ms:
                result["causes"].append("clock_offset_excessive")
            pages = connection.execute("PRAGMA page_count").fetchone()[0]
            free_pages = connection.execute("PRAGMA freelist_count").fetchone()[0]
            if (pages - free_pages) * 8192 > (metadata["policy"]["max_bytes"] - 2097152) * 0.9:
                result["causes"].append("evidence_quota_near_limit")
            oldest = connection.execute(
                "SELECT min(at) FROM records WHERE id NOT IN (SELECT record FROM pins)"
            ).fetchone()[0]
            if oldest and oldest < now - metadata["policy"]["buffer_seconds"] - 2 * interval:
                result["causes"].append("evidence_rotation_overdue")
        if disk_usage(source.database.parent).free < watch.min_free_bytes:
            result["causes"].append("local_evidence_disk_low")
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
        ObservationError,
        EvidenceError,
        RecursionError,
        AttributeError,
        OverflowError,
    ):
        result["causes"].append("evidence_source_unreadable")
        # Never expose malformed private fields read before validation.
        result["reference"] = None
        result["events"] = []
    return result


class AlertStore:
    RETENTION = 7 * 86400
    LIMIT = 4096

    def __init__(self, path, *, create=True):
        path = Path(path).absolute()
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise EvidenceError("ALERT_UNSAFE_PATH")
        if create:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = secure_path(path)
        if not path.exists():
            if not create:
                raise EvidenceError("ALERT_STORE_MISSING")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        with self.connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS health(scope TEXT PRIMARY KEY,watch TEXT,role TEXT,at REAL,
              event TEXT,incident TEXT,failed INTEGER,reference TEXT);
            CREATE TABLE IF NOT EXISTS checkpoints(scope TEXT PRIMARY KEY,at REAL);
            CREATE TABLE IF NOT EXISTS evidence(watch TEXT,id TEXT,expires REAL,
              PRIMARY KEY(watch,id));
            CREATE TABLE IF NOT EXISTS alerts(key TEXT PRIMARY KEY,status TEXT,
              changed REAL,signature TEXT,data TEXT);
            CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,at REAL,data TEXT,
              delivered REAL,attempts INTEGER DEFAULT 0);
            """)
            version = conn.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if version and version[0] != "1":
                raise EvidenceError("ALERT_VERSION_UNSUPPORTED")
            conn.execute("INSERT OR IGNORE INTO metadata VALUES('schema_version','1')")

    @contextmanager
    def connection(self):
        secure_path(self.path)
        conn = sqlite3.connect(self.path, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            if conn.execute("PRAGMA page_size").fetchone()[0] != 4096:
                raise EvidenceError("ALERT_PAGE_SIZE_INVALID")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("PRAGMA max_page_count=16384")  # 64 MiB plus bounded rollback journal.
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise EvidenceError("ALERT_STORAGE_UNAVAILABLE") from None
        finally:
            conn.close()

    def transition(self, conn, key, data, signature, now, *, failed, quiet=False):
        old = conn.execute("SELECT * FROM alerts WHERE key=?", (key,)).fetchone()
        status = "open" if failed else ("ended" if quiet else "resolved")
        if old is None and not failed:
            return
        if old and old["signature"] == signature and old["status"] == status:
            prior = json.loads(old["data"])
            prior.update(data, last_evaluated_at=stamp(now))
            conn.execute("UPDATE alerts SET data=? WHERE key=?", (canonical(prior).decode(), key))
            return
        transition = (
            "opened"
            if not old or old["status"] in {"resolved", "ended"} and failed
            else ("updated" if failed else "resolved")
        )
        event_id = digest([key, status, signature, now])
        data = {
            "schema_version": 1,
            "alert_id": digest(key),
            "event_id": event_id,
            "status": status,
            "transition": transition,
            "occurred_at": stamp(now),
            "last_evaluated_at": stamp(now),
            "first_detected_at": json.loads(old["data"])["first_detected_at"]
            if old and old["status"] == "open"
            else stamp(now),
            **data,
        }
        if len(canonical(data)) > 49152:
            raise EvidenceError("ALERT_PAYLOAD_LIMIT")
        if not quiet and conn.execute("SELECT count(*) FROM outbox").fetchone()[0] >= self.LIMIT:
            raise EvidenceError("ALERT_OUTBOX_FULL")
        conn.execute(
            (
                "INSERT INTO alerts VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE "
                "SET "
                "status=excluded.status,changed=excluded.changed,"
                "signature=excluded.signature,data=excluded.data"
            ),
            (key, status, now, signature, canonical(data).decode()),
        )
        if not quiet:
            conn.execute(
                "INSERT INTO outbox(id,at,data) VALUES(?,?,?)",
                (event_id, now, canonical(data).decode()),
            )

    def evaluate(self, config, *, now, disk_usage=shutil.disk_usage):
        snapshots = [
            (w, [inspect_source(s, w, now, disk_usage=disk_usage) for s in w.sources])
            for w in config.watches
        ]
        with self.connection() as conn:
            old_at = conn.execute(
                "SELECT value FROM metadata WHERE key='last_evaluated_at'"
            ).fetchone()
            if old_at and now < float(old_at[0]):
                raise EvidenceError("ALERT_CLOCK_REGRESSION")
            contract = digest([w.model_dump(mode="json") for w in config.watches])
            previous = conn.execute("SELECT value FROM metadata WHERE key='contract'").fetchone()
            if previous and previous[0] != contract:
                raise EvidenceError("ALERT_CONTRACT_CHANGED")
            conn.execute("INSERT OR IGNORE INTO metadata VALUES('contract',?)", (contract,))
            conn.execute(
                "DELETE FROM outbox WHERE delivered IS NOT NULL AND at<?", (now - self.RETENTION,)
            )
            conn.execute(
                ("DELETE FROM alerts WHERE status IN ('resolved','ended') AND changed<?"),
                (now - self.RETENTION,),
            )
            conn.execute("DELETE FROM evidence WHERE expires<?", (now - self.RETENTION,))
            conn.execute("DELETE FROM health WHERE failed=0 AND at<?", (now - self.RETENTION,))
            for watch, sources in snapshots:
                new_failures = {}
                active = [s for s in sources if s["active"]]
                causes = sorted({s["role"] + ":" + c for s in active for c in s["causes"]})
                offsets = [s["offset_ms"] for s in active if s["offset_ms"] is not None]
                if len(offsets) == 2 and max(offsets) - min(offsets) > watch.max_offset_spread_ms:
                    causes.append("reported_ntp_offset_spread_excessive")
                if active and watch.usage_database:
                    try:
                        with read_usage(watch.usage_database) as usage:
                            if (
                                usage.get("status") != "ok"
                                or not 0 <= now - usage["observed_at"] <= 20
                            ):
                                causes.append("usage_observation_stale")
                    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, EvidenceError):
                        causes.append("usage_observation_unreadable")
                for source in sources:
                    for incident in source["incidents"]:
                        conn.execute(
                            "INSERT INTO evidence VALUES(?,?,?) ON CONFLICT(watch,id) "
                            "DO UPDATE SET expires=max(expires,excluded.expires)",
                            (watch.id, incident["id"], incident["expires"]),
                        )
                    for row in source["events"]:
                        kind = row["kind"]
                        if kind not in {
                            "health_degraded",
                            "health_healthy",
                            "recovery_failed",
                            "protection_failed",
                            "recovery_completed",
                        }:
                            continue
                        at = timestamp(row["observed_at"])
                        if now - at > watch.evidence_fresh_seconds:
                            continue  # No stale replay presented as a new current outage/recovery.
                        checkpoint_scope = watch.id + ":" + source["role"]
                        checkpoint = conn.execute(
                            "SELECT at FROM checkpoints WHERE scope=?", (checkpoint_scope,)
                        ).fetchone()
                        if checkpoint and at <= checkpoint[0]:
                            continue
                        conn.execute(
                            "INSERT INTO checkpoints VALUES(?,?) ON CONFLICT(scope) "
                            "DO UPDATE SET at=excluded.at",
                            (checkpoint_scope, at),
                        )
                        old = conn.execute(
                            (
                                "SELECT * FROM health WHERE watch=? AND role=? ORDER BY at DESC "
                                "LIMIT 1"
                            ),
                            (watch.id, source["role"]),
                        ).fetchone()
                        if old and at <= old["at"]:
                            continue
                        failed = kind not in {"recovery_completed", "health_healthy"}
                        # A long incident can split its bounded evidence windows.
                        # Keep one ongoing health episode until a fresh recovery.
                        incident = old["incident"] if old and old["failed"] else row["incident_id"]
                        if not failed:
                            conn.execute(
                                (
                                    "UPDATE health SET failed=0,at=?,event=? WHERE watch=? "
                                    "AND role=? "
                                    "AND at<?"
                                ),
                                (at, row["event_id"], watch.id, source["role"], at),
                            )
                            continue
                        scope = watch.id + ":" + source["role"] + ":" + (incident or "unlinked")
                        new_failures.setdefault(incident or source["role"], set()).add(
                            source["role"]
                        )
                        conn.execute(
                            (
                                "INSERT INTO health VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(scope) DO "
                                "UPDATE SET "
                                "at=excluded.at,event=excluded.event,incident=excluded.incident,"
                                "failed=excluded.failed,reference=excluded.reference"
                            ),
                            (
                                scope,
                                watch.id,
                                source["role"],
                                at,
                                row["event_id"],
                                incident,
                                int(failed),
                                canonical(
                                    {
                                        "source": source["reference"],
                                        "trigger_incident_id": row["incident_id"],
                                    }
                                ).decode(),
                            ),
                        )
                expired = (
                    [
                        r[0]
                        for r in conn.execute(
                            (
                                "SELECT id FROM evidence WHERE watch=? AND expires<=? ORDER BY id "
                                "LIMIT 128"
                            ),
                            (watch.id, now),
                        )
                    ]
                    if active
                    else []
                )
                if expired:
                    causes.append("incident_evidence_expired")
                common = {
                    "software_version": __version__,
                    "producer_digest": producer_digest(),
                    "watch_id": watch.id,
                    "path_id": watch.path_id,
                    "window": {"start": stamp(now - watch.window_seconds), "end": stamp(now)},
                    "evidence": [s["reference"] for s in sources if s["reference"]],
                    "coverage": {s["role"]: s["coverage"] for s in sources},
                    "uncertainty": sorted(
                        {v for s in sources for v in s["uncertainty"]}
                        | {
                            "collector_loss_is_not_business_outage",
                            "business_impact_requires_client_evidence",
                            "disk_free_is_local_to_evaluator",
                            "offset_spread_is_reported_ntp_estimate",
                        }
                    ),
                }
                self.transition(
                    conn,
                    watch.id + ":integrity",
                    {
                        **common,
                        "category": "observation_integrity",
                        "causes": sorted(causes),
                        "expired_incident_ids": expired,
                        "next_action": (
                            "Check declared run stop/deadline, storage permissions/free "
                            "space/quota, journal coverage and time sync; preserve evidence. "
                            "Resume only with explicit authorization."
                        ),
                    },
                    digest([sorted(causes), expired]),
                    now,
                    failed=bool(causes),
                    quiet=not active,
                )
                states = [
                    dict(r) for r in conn.execute("SELECT * FROM health WHERE watch=?", (watch.id,))
                ]
                groups = {r["incident"] or r["role"] for r in states}
                # Previously opened groups remain until explicit fresh recovery.
                for group in sorted(groups):
                    members = [r for r in states if (r["incident"] or r["role"]) == group]
                    bad = sorted(r["role"] for r in members if r["failed"])
                    # Preserve a brief degradation that recovered between two evaluations.
                    if not bad and group in new_failures:
                        self.transition(
                            conn,
                            watch.id + ":health:" + group,
                            {
                                **common,
                                "category": "path_health",
                                "incident_id": group if len(group) == 32 else None,
                                "degraded_roles": sorted(new_failures[group]),
                                "causes": ["degradation_observed_before_recovery"],
                                "next_action": (
                                    "Review the failure/recovery timeline and "
                                    "confirm client impact separately."
                                ),
                            },
                            digest(sorted(new_failures[group])),
                            now,
                            failed=True,
                        )
                    self.transition(
                        conn,
                        watch.id + ":health:" + group,
                        {
                            **common,
                            "category": "path_health",
                            "incident_id": group if len(group) == 32 else None,
                            "degraded_roles": bad,
                            "last_health_evidence": {
                                r["role"]: {
                                    "observed_at": stamp(r["at"]),
                                    "age_seconds": now - r["at"],
                                    "fresh": now - r["at"] <= watch.evidence_fresh_seconds,
                                    "event_id": r["event"],
                                    "trigger_incident_id": json.loads(r["reference"]).get(
                                        "trigger_incident_id"
                                    ),
                                }
                                for r in members
                            },
                            "causes": ["fresh_degraded_or_recovery_failed"] if bad else [],
                            "next_action": (
                                "Review the incident timeline and protection/recovery evidence; "
                                "confirm client impact separately. No automatic restart or route "
                                "change."
                            ),
                        },
                        digest(
                            [
                                [r["role"], r["failed"]]
                                for r in sorted(members, key=lambda r: r["role"])
                            ]
                        ),
                        now,
                        failed=bool(bad),
                    )
            if conn.execute("SELECT count(*) FROM alerts").fetchone()[0] > self.LIMIT:
                raise EvidenceError("ALERT_STATE_FULL")
            if conn.execute("SELECT count(*) FROM health").fetchone()[0] > self.LIMIT:
                raise EvidenceError("ALERT_STATE_FULL")
            conn.execute(
                (
                    "INSERT INTO metadata VALUES('last_evaluated_at',?) ON "
                    "CONFLICT(key) DO UPDATE SET value=excluded.value"
                ),
                (str(now),),
            )
        return self.report(now=now)

    def report(self, *, now):
        with self.connection() as conn:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key='last_evaluated_at'"
            ).fetchone()
            return {
                "schema_version": 1,
                "generated_at": stamp(now),
                "last_evaluated_at": stamp(float(row[0])) if row else None,
                "evaluation_age_seconds": now - float(row[0]) if row else None,
                "outbox_expired": conn.execute(
                    "SELECT count(*) FROM outbox WHERE delivered IS NULL AND at<?",
                    (now - self.RETENTION,),
                ).fetchone()[0],
                "alerts": [
                    json.loads(r[0])
                    for r in conn.execute("SELECT data FROM alerts ORDER BY changed,key")
                ],
                "outbox_pending": conn.execute(
                    "SELECT count(*) FROM outbox WHERE delivered IS NULL"
                ).fetchone()[0],
            }

    def validate_contract(self, config):
        with self.connection() as conn:
            previous = conn.execute("SELECT value FROM metadata WHERE key='contract'").fetchone()
            expected = digest([w.model_dump(mode="json") for w in config.watches])
            if not previous or previous[0] != expected:
                raise EvidenceError("ALERT_CONTRACT_CHANGED")

    def deliver(self, channel, *, authorized, now, transport=None):
        if not authorized or channel is None:
            raise EvidenceError("ALERT_SEND_NOT_AUTHORIZED")
        headers = {"Content-Type": "application/json"}
        if channel.authorization_file:
            secret = read_private(channel.authorization_file).get("bearer_token")
            if (
                not isinstance(secret, str)
                or not secret
                or len(secret) > 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in secret)
            ):
                raise EvidenceError("ALERT_CHANNEL_INVALID")
            headers["Authorization"] = "Bearer " + secret
        # Explicit delivery is bounded to 8 pending events, one attempt each per invocation.
        sent, failed = 0, 0
        deadline = time.monotonic() + 20
        with self.connection() as conn:
            for row in conn.execute(
                "SELECT * FROM outbox WHERE delivered IS NULL AND at>=? ORDER BY at,id LIMIT 8",
                (now - self.RETENTION,),
            ).fetchall():
                if time.monotonic() >= deadline:
                    break
                if now - row["at"] > self.RETENTION:
                    continue  # Stale unsent messages remain visible; never replay as a fresh alert.
                try:
                    if channel.kind == "directory":
                        destination = Path(channel.destination) / (row["id"] + ".json")
                        if destination.exists():
                            if read_private(secure_path(destination)) != json.loads(row["data"]):
                                raise EvidenceError()
                        else:
                            export_json(json.loads(row["data"]), destination)
                    else:
                        with (
                            httpx.Client(
                                transport=transport,
                                trust_env=False,
                                follow_redirects=False,
                                timeout=3,
                            ) as client,
                            client.stream(
                                "POST",
                                channel.destination,
                                content=row["data"],
                                headers={**headers, "Idempotency-Key": row["id"]},
                            ) as response,
                        ):
                            # Never persist remote response data or exception strings.
                            if not 200 <= response.status_code < 300:
                                raise EvidenceError()
                    conn.execute(
                        "UPDATE outbox SET delivered=?,attempts=attempts+1 WHERE id=?",
                        (now, row["id"]),
                    )
                    sent += 1
                except (OSError, ValueError, ObservationError, EvidenceError, httpx.HTTPError):
                    conn.execute("UPDATE outbox SET attempts=attempts+1 WHERE id=?", (row["id"],))
                    failed += 1
        expired = self.report(now=now)["outbox_expired"]
        return {
            "status": "delivery_failed"
            if failed
            else ("expired_pending" if expired else "delivered"),
            "sent": sent,
            "failed": failed,
            "expired_pending": expired,
            "semantics": "at_least_once; receiver_should_deduplicate_by_event_id",
        }

    def discard_expired(self, *, now):
        """Explicit local retention operation. Never delete fresh pending notifications."""
        with self.connection() as conn:
            count = conn.execute(
                "DELETE FROM outbox WHERE delivered IS NULL AND at<?", (now - self.RETENTION,)
            ).rowcount
            old = conn.execute(
                "SELECT value FROM metadata WHERE key='discarded_expired'"
            ).fetchone()
            conn.execute(
                "INSERT INTO metadata VALUES('discarded_expired',?) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value",
                (str(count + (int(old[0]) if old else 0)),),
            )
        return {"status": "expired_notifications_discarded", "count": count}


@contextmanager
def read_usage(path):
    path = secure_path(path)
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        conn.execute("PRAGMA query_only=ON")
        row = conn.execute("SELECT value FROM settings WHERE key='usage_state'").fetchone()
        yield json.loads(row[0]) if row else {}
    finally:
        conn.close()
