"""Independent bounded log database. Queries never migrate or mutate sources."""

import base64
import ipaddress
import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from wayvia.observability.files import directory, initialize, locked, policy, update_policy
from wayvia.observability.models import (
    DAY,
    Event,
    LogError,
    QueryFilters,
    canonical,
    digest,
    domain,
)
from wayvia.telemetry.store import secure_path

SCHEMA = """
CREATE TABLE metadata(version INTEGER NOT NULL);
INSERT INTO metadata VALUES(1);
CREATE TABLE records(id TEXT PRIMARY KEY,kind TEXT NOT NULL,at REAL NOT NULL,
 received REAL NOT NULL,expires REAL NOT NULL,host TEXT NOT NULL,role TEXT NOT NULL,
 source TEXT NOT NULL,device TEXT,source_ip TEXT,target_host TEXT,target_ip TEXT,
 status INTEGER,code TEXT NOT NULL,data TEXT NOT NULL);
CREATE INDEX records_time ON records(kind,at,id);
CREATE INDEX records_device ON records(device,kind,at);
CREATE INDEX records_expiry ON records(expires);
CREATE TABLE sources(id TEXT PRIMARY KEY,host TEXT,source TEXT,epoch TEXT,
 last_at REAL,received REAL,last_sequence INTEGER,state TEXT,code TEXT);
CREATE TABLE offsets(id TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE rollups(day INTEGER NOT NULL,host TEXT NOT NULL,status INTEGER NOT NULL,
 reason TEXT NOT NULL,count INTEGER NOT NULL,PRIMARY KEY(day,host,status,reason));
"""


def request_reason(data):
    if data.get("limit_rate") == "REJECTED":
        return "rate_limit"
    if data.get("limit_concurrent") == "REJECTED":
        return "concurrency_limit"
    status = data.get("status", 200)
    return (
        "unknown_429"
        if status == 429
        else "upstream_error"
        if status >= 500
        else "client_closed"
        if status == 499
        else "denied"
        if status in {401, 403}
        else "none"
    )


class LogStore:
    def __init__(self, root, *, create=False):
        self.root = Path(root).absolute()
        if create:
            directory(self.root)
            initialize(self.root)
        self.path = secure_path(self.root / "observability.db")
        if create:
            with locked(self.root / "initialize.lock"):
                if not self.path.exists():
                    fd = os.open(
                        self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
                    )
                    os.close(fd)
                    with self.connect(write=True, initializing=True) as con:
                        con.executescript(SCHEMA)

    def policy(self):
        return policy(self.root).model_dump()

    def update_policy(self, changes, expected_revision):
        if changes.get("max_data_bytes", 268435456) < self.path.stat().st_size:
            raise LogError("LOG_CAPACITY_BELOW_CURRENT", status=409)
        return update_policy(self.root, changes, expected_revision).model_dump()

    @contextmanager
    def connect(self, *, write=False, initializing=False):
        secure_path(self.path)
        config = policy(self.root)
        if not self.path.exists():
            raise LogError("LOG_NOT_INITIALIZED", status=503)
        con = sqlite3.connect(
            self.path if write else self.path.as_uri() + "?mode=ro", uri=not write, timeout=0.25
        )
        con.row_factory = sqlite3.Row
        deadline = time.monotonic() + 2
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        con.create_function(
            "in_cidr",
            2,
            lambda value, network: int(
                bool(value) and ipaddress.ip_address(value) in ipaddress.ip_network(network)
            ),
        )
        try:
            if not initializing and con.execute("SELECT version FROM metadata").fetchone()[0] != 1:
                raise LogError("LOG_SCHEMA_UNSUPPORTED", status=503)
            if write:
                con.execute("PRAGMA journal_mode=DELETE")
                con.execute("PRAGMA synchronous=FULL")
                con.execute("PRAGMA secure_delete=ON")
                page_size = con.execute("PRAGMA page_size").fetchone()[0]
                con.execute(f"PRAGMA max_page_count={config.max_data_bytes // page_size}")
                con.execute("BEGIN IMMEDIATE")
            else:
                con.execute("PRAGMA query_only=ON")
                if con.execute("SELECT version FROM metadata").fetchone()[0] != 1:
                    raise LogError("LOG_SCHEMA_UNSUPPORTED", status=503)
            yield con
            if write:
                con.commit()
        except sqlite3.Error:
            con.rollback()
            raise LogError("LOG_STORAGE_UNAVAILABLE", status=503) from None
        finally:
            con.close()

    def offset(self, key):
        with self.connect() as con:
            row = con.execute("SELECT value FROM offsets WHERE id=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def ingest(self, events, *, checkpoint=None, now=None):
        now = time.time() if now is None else now
        config = policy(self.root)
        if not config.desired_enabled:
            raise LogError("LOG_DISABLED", status=409)
        if len(events) > 200:
            raise LogError("LOG_BATCH_TOO_LARGE")
        events = [Event.model_validate(e) if not isinstance(e, Event) else e for e in events]
        if any(config.hosts.get(e.host_id) != e.role or e.observed_at > now + 60 for e in events):
            raise LogError("LOG_SOURCE_INVALID")
        disk = shutil.disk_usage(self.root)
        if disk.free < max(config.min_free_bytes, disk.total // 10 if config.min_free_bytes else 0):
            raise LogError("LOG_STORAGE_FULL", status=503)
        with self.connect(write=True) as con:
            self._prune(con, now, config)
            for item in events:
                self._source(con, item, now)
                if (
                    item.kind == "heartbeat"
                    or item.observed_at + config.retention(item.kind) <= now
                ):
                    continue
                data = item.data()
                if item.kind == "request":
                    key = digest(["request", item.host_id, item.request_id])
                elif item.kind == "connection":
                    key = digest(["connection", item.host_id, item.core_epoch, item.connection_id])
                else:
                    key = digest([item.host_id, item.source, item.event_id])
                old = con.execute("SELECT * FROM records WHERE id=?", (key,)).fetchone()
                if old:
                    previous = json.loads(old["data"])
                    if item.kind == "request":
                        # Nginx is authoritative for edge status, source and timing.
                        if previous.get("edge_observed") and item.source == "api":
                            data = previous | {
                                k: v
                                for k, v in data.items()
                                if item.device_id
                                and k in {"device_id", "subscription_id", "identity_source"}
                            }
                        else:
                            data = previous | data
                            if previous.get("device_id") and not item.device_id:
                                data.update(
                                    {
                                        k: previous[k]
                                        for k in ("device_id", "subscription_id", "identity_source")
                                        if k in previous
                                    }
                                )
                    elif item.kind == "connection":
                        if previous.get("device_id") != item.device_id:
                            raise LogError("LOG_IDENTITY_CONFLICT")
                        if previous["observed_at"] > item.observed_at:
                            continue
                        for field in ("upload_bytes", "download_bytes"):
                            if data.get(field, 0) < previous.get(field, 0):
                                data["code"] = "counter_reset"
                                data[field] = previous[field]
                        if previous.get("lifecycle") == "closed":
                            data["lifecycle"] = "closed"
                            data["closed_at"] = previous.get("closed_at")
                    else:
                        continue
                if item.kind == "request":
                    data["edge_observed"] = bool(
                        data.get("edge_observed") or item.source == "nginx"
                    )
                    data["code"] = request_reason(data)
                    # Rollups count canonical records, including later edge correction.
                    if old:
                        previous = json.loads(old["data"])
                        self._rollup(con, previous, -1)
                    self._rollup(con, data, 1)
                if item.kind == "connection":
                    old_window = previous.get("counter_window") if old else None
                    if old_window:
                        data["counter_window"] = old_window
                    if (data.get("created_at") or now) < now - config.retention(item.kind):
                        if not old_window or old_window["at"] < now - config.retention(item.kind):
                            data["counter_window"] = {
                                "at": now,
                                "upload": data.get("upload_bytes", 0),
                                "download": data.get("download_bytes", 0),
                            }
                        data["code"] = "missing_start"
                expiry = min(data["observed_at"], now) + config.retention(item.kind)
                if item.kind == "connection" and data.get(
                    "created_at", now
                ) < now - config.retention(item.kind):
                    data["created_at"] = None
                    data["code"] = "missing_start"
                con.execute(
                    "INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        item.kind,
                        data["observed_at"],
                        now,
                        expiry,
                        item.host_id,
                        item.role,
                        data["source"],
                        data.get("device_id"),
                        data.get("source_ip"),
                        data.get("target_host"),
                        data.get("target_ip"),
                        data.get("status"),
                        data["code"],
                        canonical(data),
                    ),
                )
            if checkpoint:
                key, value = checkpoint
                if len(key) > 128 or len(canonical(value)) > 16384:
                    raise LogError("LOG_CHECKPOINT_INVALID")
                con.execute("INSERT OR REPLACE INTO offsets VALUES(?,?)", (key, canonical(value)))
        return len(events)

    def _rollup(self, con, data, delta):
        con.execute(
            "INSERT INTO rollups VALUES(?,?,?,?,?) ON CONFLICT(day,host,status,reason) "
            "DO UPDATE SET count=count+excluded.count",
            (
                int(data["observed_at"] // DAY),
                data["host_id"],
                data["status"],
                request_reason(data),
                delta,
            ),
        )

    def _source(self, con, item, now):
        key = f"{item.host_id}:{item.source}"
        old = con.execute("SELECT * FROM sources WHERE id=?", (key,)).fetchone()
        if old and old["last_at"] > item.observed_at:
            return
        state = "degraded" if item.kind in {"gap", "source_error"} else "observed"
        con.execute(
            "INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?,?,?,?)",
            (
                key,
                item.host_id,
                item.source,
                item.source_epoch,
                item.observed_at,
                now,
                item.sequence,
                state,
                item.code,
            ),
        )

    def _prune(self, con, now, config):
        con.execute("DELETE FROM records WHERE expires<=?", (now,))
        # A shortened policy also applies to existing records immediately.
        from wayvia.observability.models import KINDS

        for kind in KINDS:
            seconds = config.retention(kind)
            con.execute("DELETE FROM records WHERE kind=? AND at<?", (kind, now - seconds))
        con.execute("DELETE FROM rollups WHERE day<?", (int(now // DAY) - 90,))

    def prune(self, *, now=None):
        with self.connect(write=True) as con:
            self._prune(con, time.time() if now is None else now, policy(self.root))

    def sources(self, *, now=None):
        now = time.time() if now is None else now
        config = policy(self.root)
        with self.connect() as con:
            found = {r["id"]: dict(r) for r in con.execute("SELECT * FROM sources")}
        result = []
        for host, role in config.hosts.items():
            expected = (
                ("system", "audit", "connections", "nginx", "api")
                if role == "ingress"
                else ("system", "transfer")
            )
            for source in expected:
                key = f"{host}:{source}"
                row = found.get(
                    key,
                    {"id": key, "host": host, "source": source, "last_at": None, "received": None},
                )
                row["state"] = (
                    "disabled"
                    if not config.desired_enabled
                    else "unavailable"
                    if row["received"] is None
                    else "stale"
                    if max(now - row["received"], now - row["last_at"])
                    > (90 if role == "exit" else 20)
                    else row.get("state", "unknown")
                )
                row["role"] = role
                row["coverage_scope"] = "best_effort_events; no_proof_of_lossless_delivery"
                result.append(row)
        state_path = self.root / "collector-state.json"
        if state_path.exists():
            from wayvia.observability.files import read_json

            state = read_json(state_path)
            result.append(
                {
                    "id": "collector",
                    "host": "local",
                    "role": "ingress",
                    "source": "collector",
                    "last_at": state.get("at"),
                    "received": state.get("at"),
                    "state": "disabled"
                    if not config.desired_enabled
                    else "stale"
                    if now - state.get("at", 0) > 20
                    else "degraded"
                    if state.get("storage")
                    or state.get("dropped")
                    or state.get("invalid")
                    or state.get("dropped_batches")
                    else "observed",
                    "code": "storage_full"
                    if state.get("storage")
                    else "queue_full"
                    if state.get("dropped")
                    else "buffer_full"
                    if state.get("dropped_batches")
                    else "parse_failed"
                    if state.get("invalid")
                    else "none",
                    "dropped": state.get("dropped", 0),
                    "invalid": state.get("invalid", 0),
                    "dropped_batches": state.get("dropped_batches", 0),
                }
            )
        return result

    def query(self, filters=None, *, cursor=None, limit=100, now=None):
        try:
            filters = QueryFilters.model_validate(filters or {}).model_dump(exclude_none=True)
        except ValueError:
            raise LogError("LOG_QUERY_INVALID") from None
        now = time.time() if now is None else now
        allowed = {
            "since",
            "until",
            "kind",
            "category",
            "device_id",
            "role",
            "host_id",
            "source_ip",
            "target",
            "status",
            "code",
        }
        if set(filters) - allowed or not 1 <= limit <= 500:
            raise LogError("LOG_QUERY_INVALID")
        if cursor and ("since" not in filters or "until" not in filters or len(cursor) > 512):
            raise LogError("LOG_CURSOR_INVALID")
        since, until = float(filters.get("since", now - 3600)), float(filters.get("until", now))
        if not 0 <= since < until <= now + 60 or until - since > DAY:
            raise LogError("LOG_QUERY_WINDOW")
        config = policy(self.root)
        conditions = [
            "at>=?",
            "at<?",
            "expires>?",
            (
                "at>=? - CASE WHEN kind='connection' THEN ? "
                "WHEN kind IN ('request','ssh_accepted','ssh_failed') THEN ? "
                "WHEN kind='management' THEN ? ELSE ? END"
            ),
        ]
        values = [
            since,
            until,
            now,
            now,
            config.connection_days * DAY,
            config.request_days * DAY,
            config.audit_days * DAY,
            config.system_days * DAY,
        ]
        if filters.get("category") == "system":
            conditions.append("kind NOT IN ('request','connection','management')")
        elif filters.get("category") is not None:
            raise LogError("LOG_QUERY_INVALID")
        for name, col in {
            "kind": "kind",
            "device_id": "device",
            "role": "role",
            "host_id": "host",
            "status": "status",
            "code": "code",
        }.items():
            if filters.get(name) is not None:
                if len(str(filters[name])) > 128:
                    raise LogError("LOG_QUERY_INVALID")
                conditions.append(f"{col}=?")
                values.append(filters[name])
        if filters.get("source_ip"):
            network = str(ipaddress.ip_network(filters["source_ip"], strict=False))
            conditions.append("in_cidr(source_ip,?)")
            values.append(network)
        if filters.get("target"):
            value = filters["target"]
            try:
                address = str(ipaddress.ip_address(value))
                conditions.append("target_ip=?")
                values.append(address)
            except ValueError:
                host = domain(value)
                conditions.append("(target_host=? OR target_host LIKE ?)")
                values.extend([host, "%." + host])
        with self.connect() as con:
            where = " AND ".join(conditions)
            summary = con.execute(
                (
                    "SELECT COUNT(*) total,SUM(CASE WHEN code IN "
                    "('rate_limit','concurrency_limit','unknown_429') THEN 1 ELSE 0 END) limited,"
                    "MIN(at) first FROM records WHERE "
                )
                + where,
                values,
            ).fetchone()
            if cursor:
                try:
                    at, ident, contract = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                    if (
                        contract != digest(filters)
                        or not isinstance(at, (float, int))
                        or not isinstance(ident, str)
                        or len(ident) != 64
                    ):
                        raise ValueError()
                except Exception:
                    raise LogError("LOG_CURSOR_INVALID") from None
                conditions.append("(at<? OR (at=? AND id<?))")
                values.extend([at, at, ident])
            rows = list(
                con.execute(
                    "SELECT id,at,data FROM records WHERE "
                    + " AND ".join(conditions)
                    + " ORDER BY at DESC,id DESC LIMIT ?",
                    values + [limit + 1],
                )
            )
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            base64.urlsafe_b64encode(
                canonical([rows[-1]["at"], rows[-1]["id"], digest(filters)]).encode()
            ).decode()
            if more
            else None
        )
        return {
            "items": [
                self._visible(json.loads(r["data"]), config, now) | {"record_id": r["id"]}
                for r in rows
            ],
            "next_cursor": next_cursor,
            "as_of": now,
            "total": summary["total"],
            "limited": summary["limited"] or 0,
            "earliest_available_at": summary["first"],
            "truncated": False,
            "coverage": {"scope": "best_effort_observed_events", "sources": self.sources(now=now)},
            "window": {"since": since, "until": until},
        }

    @staticmethod
    def _visible(data, config, now):
        if data["kind"] == "connection":
            window = data.pop("counter_window", None)
            data["byte_scope"] = "connection_cumulative_at_last_observation"
            if window:
                data["byte_scope"] = "observed_retention_window_delta"
                data["window_started_at"] = window["at"]
                for direction in ("upload", "download"):
                    field = direction + "_bytes"
                    data[field] = max(0, data.get(field, 0) - window[direction])
            if (data.get("created_at") or now) < now - config.retention("connection"):
                data["created_at"] = None
                if not window:
                    data["upload_bytes"] = data["download_bytes"] = None
                    data["byte_scope"] = "outside_retained_window_unknown"
            if data.get("lifecycle") == "active" and (
                not config.desired_enabled or now - data["observed_at"] > 20
            ):
                data.update(lifecycle="unknown", code="missing_final")
        return data

    def rollups(self, since, until):
        now = time.time()
        if not now - 90 * DAY <= since < until <= now or until - since > 90 * DAY:
            raise LogError("LOG_QUERY_WINDOW")
        with self.connect() as con:
            rows = con.execute(
                "SELECT * FROM rollups WHERE day>=? AND day<? AND count>0 ORDER BY day",
                (int(since // DAY), int(until // DAY) + 1),
            )
            return {
                "items": [dict(r) for r in rows],
                "scope": "whole_utc_days_observed_requests",
                "coverage": self.sources(),
            }
