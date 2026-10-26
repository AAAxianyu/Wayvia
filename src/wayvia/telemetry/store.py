"""A private, page-bounded SQLite ring with incident pins and explicit retention.

Only unpinned ring records and expired incidents are removed. A full store stops
diagnostics rather than discarding unexpired incident evidence or changing health.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from wayvia.observation_run import ObservationError, private
from wayvia.telemetry.records import (
    EvidenceError,
    Policy,
    canonical,
    digest,
    project,
    stamp,
    timestamp,
)

SCHEMA = """
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE records(id TEXT PRIMARY KEY,at REAL NOT NULL,role TEXT,kind TEXT,data TEXT NOT NULL);
CREATE INDEX records_time ON records(at);
CREATE TABLE incidents(id TEXT PRIMARY KEY,trigger_at REAL NOT NULL,start REAL NOT NULL,
 end REAL NOT NULL,expires REAL NOT NULL,origin_role TEXT,origin_generation TEXT);
CREATE TABLE pins(incident TEXT REFERENCES incidents(id) ON DELETE CASCADE,
 record TEXT REFERENCES records(id) ON DELETE CASCADE,PRIMARY KEY(incident,record));
CREATE TABLE triggers(record TEXT PRIMARY KEY,
 incident TEXT REFERENCES incidents(id) ON DELETE CASCADE);
CREATE TABLE progress(key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""
TRIGGERS = {
    "probe_failed",
    "health_degraded",
    "protection_requested",
    "protection_failed",
    "business_failure",
}


def secure_path(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise EvidenceError("EVIDENCE_UNSAFE_PATH")
    try:
        private(path.parent, directory=True)
        if path.exists():
            private(path)
    except (ObservationError, OSError):
        raise EvidenceError("EVIDENCE_UNSAFE_PATH") from None
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            private(sidecar)
    return path


@contextmanager
def read_database(path):
    import json

    path = secure_path(path)
    if not path.exists():
        raise EvidenceError("EVIDENCE_STORE_MISSING")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        metadata = {
            r[0]: json.loads(r[1]) for r in connection.execute("SELECT key,value FROM metadata")
        }
        if metadata.get("schema_version") != 1:
            raise EvidenceError("EVIDENCE_VERSION_UNSUPPORTED")
        yield connection, metadata
    except sqlite3.Error:
        raise EvidenceError("EVIDENCE_STORE_INVALID") from None
    finally:
        connection.close()


class DiagnosticStore:
    def __init__(self, directory, metadata, policy=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = secure_path(self.directory / "diagnostics.db")
        self.policy = policy or Policy()
        from wayvia.evidence import token

        if (
            set(metadata) != {"role", "revision", "generation", "path_id", "run_id"}
            or metadata["role"] not in {"ingress", "exit"}
            or token(metadata["revision"], r"[a-f0-9]{64}") is None
            or token(metadata["generation"], r"[a-f0-9]{32}") is None
            or any(token(metadata[k], r"[a-zA-Z0-9-]{1,64}") is None for k in ("path_id", "run_id"))
        ):
            raise EvidenceError("EVIDENCE_METADATA_INVALID")
        self.metadata = {"schema_version": 1, **metadata, "policy": self.policy.model_dump()}
        new = not self.path.exists()
        if new:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        with self.write() as connection:
            if new:
                connection.executescript(SCHEMA)
                connection.executemany(
                    "INSERT INTO metadata VALUES(?,?)",
                    [(key, canonical(value).decode()) for key, value in self.metadata.items()],
                )
            actual = {
                r[0]: json.loads(r[1]) for r in connection.execute("SELECT key,value FROM metadata")
            }
            if actual != self.metadata:
                raise EvidenceError("EVIDENCE_CONTRACT_CHANGED")

    @classmethod
    def open(cls, path):
        if Path(path).name != "diagnostics.db":
            raise EvidenceError("EVIDENCE_STORE_NAME_INVALID")
        with read_database(path) as (_, metadata):
            policy = Policy.model_validate(metadata.pop("policy"))
        metadata.pop("schema_version")
        return cls(Path(path).parent, metadata, policy)

    @contextmanager
    def write(self):
        secure_path(self.path)
        connection = sqlite3.connect(self.path, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA page_size=4096")
            if connection.execute("PRAGMA page_size").fetchone()[0] != 4096:
                raise EvidenceError("EVIDENCE_PAGE_SIZE_INVALID")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA secure_delete=ON")
            # Reserve another database-sized allowance for the rollback journal,
            # plus two MiB for lifecycle metadata and trigger transfer files.
            pages = (self.policy.max_bytes - 2097152) // (2 * 4096)
            connection.execute(f"PRAGMA max_page_count={pages}")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            raise EvidenceError("EVIDENCE_STORAGE_UNAVAILABLE") from None
        finally:
            connection.close()

    def _prune(self, connection, now):
        connection.execute("DELETE FROM incidents WHERE expires<=?", (now,))
        connection.execute(
            "DELETE FROM records WHERE at<? AND id NOT IN (SELECT record FROM pins)",
            (now - self.policy.buffer_seconds,),
        )

    def append(self, row, *, now=None, client_authorized=False):
        clean = project(row, client_authorized=client_authorized)
        if clean["role"] not in {self.metadata["role"], "unknown"}:
            raise EvidenceError("EVIDENCE_ROLE_MISMATCH")
        at = timestamp(clean["observed_at"])
        now = at if now is None else now
        if at > now + 5:
            raise EvidenceError("EVIDENCE_FUTURE_TIME")
        raw = canonical(clean)
        if len(raw) > 65536:
            raise EvidenceError("EVIDENCE_RECORD_TOO_LARGE")
        with self.write() as connection:
            self._prune(connection, now)
            inserted = connection.execute(
                "INSERT OR IGNORE INTO records VALUES(?,?,?,?,?)",
                (clean["event_id"], at, clean["role"], clean.get("kind", "sample"), raw.decode()),
            ).rowcount
            if not inserted:
                previous = connection.execute(
                    "SELECT data FROM records WHERE id=?", (clean["event_id"],)
                ).fetchone()[0]
                if previous != raw.decode():
                    raise EvidenceError("EVIDENCE_ID_CONFLICT")
                return None
            connection.execute(
                "INSERT OR IGNORE INTO pins SELECT id,? FROM incidents WHERE start<=? AND end>=?",
                (clean["event_id"], at, at),
            )
            incident = None
            if (
                clean.get("kind") in TRIGGERS
                and clean["revision"] == self.metadata["revision"]
                and clean["generation"] == self.metadata["generation"]
                and clean["role"] == self.metadata["role"]
            ):
                existing = connection.execute(
                    "SELECT * FROM incidents WHERE end>=? AND start<=? "
                    "ORDER BY trigger_at DESC LIMIT 1",
                    (at, at),
                ).fetchone()
                if (
                    existing
                    and at - existing["trigger_at"] < 1200
                    and max(existing["end"], at + self.policy.post_seconds)
                    - min(existing["start"], at - self.policy.pre_seconds)
                    <= 1800
                ):
                    incident = existing["id"]
                    start = min(existing["start"], at - self.policy.pre_seconds)
                    end = min(
                        start + 1800,
                        max(existing["end"], at + self.policy.post_seconds),
                    )
                    connection.execute(
                        "UPDATE incidents SET trigger_at=min(trigger_at,?),start=?,end=?,"
                        "expires=max(expires,?) WHERE id=?",
                        (at, start, end, end + self.policy.retention_seconds, incident),
                    )
                else:
                    incident = digest([self.metadata["path_id"], clean["event_id"]])[:32]
                    self._trigger(
                        connection,
                        {
                            "incident_id": incident,
                            "at": at,
                            "origin_role": clean["role"],
                            "origin_generation": clean["generation"],
                            "start": at - self.policy.pre_seconds,
                            "end": at + self.policy.post_seconds,
                        },
                        now,
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO triggers VALUES(?,?)", (clean["event_id"], incident)
                )
            if clean.get("kind") == "recovery_completed":
                connection.execute(
                    "UPDATE incidents SET end=min(start+1800,max(end,?)) "
                    "WHERE end>=? AND trigger_at<=?",
                    (at + self.policy.post_seconds, at, at),
                )
                connection.execute(
                    "UPDATE incidents SET expires=max(expires,end+?)",
                    (self.policy.retention_seconds,),
                )
            # Pin records that arrived out of order before a trigger/extension.
            connection.execute(
                "INSERT OR IGNORE INTO pins SELECT i.id,r.id FROM incidents i "
                "JOIN records r ON r.at BETWEEN i.start AND i.end"
            )
            return incident

    def _trigger(self, connection, trigger, now):
        count = connection.execute("SELECT count(*) FROM incidents").fetchone()[0]
        existing = connection.execute(
            "SELECT * FROM incidents WHERE id=?", (trigger["incident_id"],)
        ).fetchone()
        if existing:
            if (
                existing["origin_role"] != trigger["origin_role"]
                or existing["origin_generation"] != trigger["origin_generation"]
                or max(existing["end"], trigger["end"]) - min(existing["start"], trigger["start"])
                > 1800
            ):
                raise EvidenceError("EVIDENCE_TRIGGER_CONFLICT")
            connection.execute(
                "UPDATE incidents SET trigger_at=min(trigger_at,?),start=min(start,?),"
                "end=max(end,?),expires=max(expires,?) WHERE id=?",
                (
                    trigger["at"],
                    trigger["start"],
                    trigger["end"],
                    trigger["end"] + self.policy.retention_seconds,
                    trigger["incident_id"],
                ),
            )
        else:
            if count >= self.policy.max_incidents:
                raise EvidenceError("EVIDENCE_INCIDENT_CAPACITY")
            connection.execute(
                "INSERT INTO incidents VALUES(?,?,?,?,?,?,?)",
                (
                    trigger["incident_id"],
                    trigger["at"],
                    trigger["start"],
                    trigger["end"],
                    trigger["end"] + self.policy.retention_seconds,
                    trigger["origin_role"],
                    trigger["origin_generation"],
                ),
            )
        connection.execute(
            "INSERT OR IGNORE INTO pins SELECT ?,id FROM records WHERE at BETWEEN ? AND ?",
            (trigger["incident_id"], trigger["start"], trigger["end"]),
        )

    def triggers(self):
        with read_database(self.path) as (connection, _):
            return [
                {
                    "schema_version": 1,
                    "path_id": self.metadata["path_id"],
                    "revision": self.metadata["revision"],
                    "incident_id": r["id"],
                    "at": stamp(r["trigger_at"]),
                    "start": stamp(r["start"]),
                    "end": stamp(r["end"]),
                    "origin_role": r["origin_role"],
                    "origin_generation": r["origin_generation"],
                }
                for r in connection.execute("SELECT * FROM incidents ORDER BY trigger_at")
            ]

    def import_trigger(self, value, *, now):
        from wayvia.evidence import token

        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or value.get("path_id") != self.metadata["path_id"]
            or value.get("revision") != self.metadata["revision"]
            or token(value.get("incident_id"), r"[a-f0-9]{32}") is None
            or value.get("origin_role") not in {"ingress", "exit"}
            or token(value.get("origin_generation"), r"[a-f0-9]{32}") is None
        ):
            raise EvidenceError("EVIDENCE_TRIGGER_INVALID")
        at, start, end = (timestamp(value.get(key)) for key in ("at", "start", "end"))
        if (
            at > now + 5
            or at < now - self.policy.retention_seconds
            or start > at - 120
            or end < at + 120
            or end - start > 1800
            or at - start > 600
        ):
            raise EvidenceError("EVIDENCE_TRIGGER_WINDOW_INVALID")
        with self.write() as connection:
            self._prune(connection, now)
            self._trigger(connection, {**value, "at": at, "start": start, "end": end}, now)

    def checkpoint(self, key, value):
        if key not in {"journal_since", "last_sample", "audit_since", "gaps"}:
            raise EvidenceError()
        with self.write() as connection:
            connection.execute(
                "INSERT INTO progress VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, canonical(value).decode()),
            )

    def progress(self):
        with read_database(self.path) as (connection, _):
            return {
                r[0]: json.loads(r[1]) for r in connection.execute("SELECT key,value FROM progress")
            }

    def prune(self, now):
        with self.write() as connection:
            self._prune(connection, now)
