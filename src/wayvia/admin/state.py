"""Private sessions and durable administrative intents, separate from traffic logs."""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from wayvia.observability.files import directory, locked
from wayvia.observability.models import LogError, canonical
from wayvia.telemetry.store import secure_path

SCHEMA = """
CREATE TABLE metadata(version INTEGER); INSERT INTO metadata VALUES(1);
CREATE TABLE settings(key TEXT PRIMARY KEY,value INTEGER);
INSERT INTO settings VALUES('audit_days',90);
CREATE TABLE codes(hash TEXT PRIMARY KEY,expires REAL);
CREATE TABLE sessions(id TEXT PRIMARY KEY,hash TEXT UNIQUE,csrf TEXT,created REAL,last REAL);
CREATE TABLE attempts(at REAL);
CREATE TABLE audit(id TEXT PRIMARY KEY,at REAL,session TEXT,action TEXT,object TEXT,result TEXT);
CREATE INDEX audit_time ON audit(at);
CREATE TABLE actions(id TEXT PRIMARY KEY,session TEXT,action TEXT,object TEXT,
 payload_hash TEXT,result TEXT,at REAL);
CREATE TABLE exports(id TEXT PRIMARY KEY,session TEXT,created REAL,expires REAL,
 size INTEGER,digest TEXT,mode TEXT);
CREATE TABLE acknowledgements(id TEXT PRIMARY KEY,at REAL,session TEXT);
"""


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()


class AdminState:
    def __init__(self, root, *, create=False):
        self.root = Path(root).absolute()
        if create:
            directory(self.root)
        self.path = secure_path(self.root / "admin.db")
        if create:
            with locked(self.root / "initialize.lock"):
                if not self.path.exists():
                    fd = os.open(
                        self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
                    )
                    os.close(fd)
                    with self.connect(initializing=True) as con:
                        con.executescript(SCHEMA)
            directory(self.root / "exports")

    @contextmanager
    def connect(self, *, initializing=False):
        secure_path(self.path)
        if not self.path.exists():
            raise LogError("ADMIN_NOT_INITIALIZED", status=503)
        con = sqlite3.connect(self.path, timeout=0.25)
        con.row_factory = sqlite3.Row
        end = time.monotonic() + 2
        con.set_progress_handler(lambda: int(time.monotonic() > end), 1000)
        try:
            con.execute("PRAGMA journal_mode=DELETE")
            con.execute("PRAGMA synchronous=FULL")
            con.execute("PRAGMA secure_delete=ON")
            con.execute("PRAGMA max_page_count=8192")  # 32 MiB + bounded rollback journal.
            if not initializing and con.execute("SELECT version FROM metadata").fetchone()[0] != 1:
                raise LogError("ADMIN_SCHEMA_UNSUPPORTED", status=503)
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except sqlite3.Error:
            con.rollback()
            raise LogError("ADMIN_STATE_UNAVAILABLE", status=503) from None
        finally:
            con.close()

    @staticmethod
    def record(con, session, action, obj, result):
        con.execute(
            "INSERT INTO audit VALUES(?,?,?,?,?,?)",
            (uuid.uuid4().hex, time.time(), session, action, obj, result),
        )

    def code(self):
        value = secrets.token_urlsafe(24)
        with self.connect() as con:
            con.execute("DELETE FROM codes")
            con.execute("INSERT INTO codes VALUES(?,?)", (hashed(value), time.time() + 60))
        return value

    def login(self, code):
        now = time.time()
        token, csrf, ident = secrets.token_urlsafe(32), secrets.token_urlsafe(32), uuid.uuid4().hex
        accepted = False
        limited = False
        with self.connect() as con:
            con.execute("DELETE FROM attempts WHERE at<?", (now - 60,))
            limited = con.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] >= 5
            if not limited:
                con.execute("INSERT INTO attempts VALUES(?)", (now,))
                row = con.execute(
                    "SELECT expires FROM codes WHERE hash=?", (hashed(code),)
                ).fetchone()
                accepted = bool(row and row[0] > now)
                if accepted:
                    con.execute("DELETE FROM codes")
                    con.execute(
                        "INSERT INTO sessions VALUES(?,?,?,?,?)",
                        (ident, hashed(token), hashed(csrf), now, now),
                    )
            if not limited:
                self.record(
                    con,
                    ident if accepted else "anonymous",
                    "session.login",
                    None,
                    "success" if accepted else "failed",
                )
        if not accepted:
            raise LogError(
                "ADMIN_LOGIN_LIMIT" if limited else "ADMIN_LOGIN_INVALID",
                status=429 if limited else 401,
            )
        return token, csrf, ident

    def session(self, token, csrf=None, *, write=False):
        if not isinstance(token, str) or len(token) > 128:
            raise LogError("ADMIN_SESSION_REQUIRED", status=401)
        now = time.time()
        with self.connect() as con:
            row = con.execute("SELECT * FROM sessions WHERE hash=?", (hashed(token),)).fetchone()
            if row is None or row["last"] < now - 1800 or row["created"] < now - 28800:
                raise LogError("ADMIN_SESSION_EXPIRED", status=401)
            if write and (
                not isinstance(csrf, str) or not hmac.compare_digest(row["csrf"], hashed(csrf))
            ):
                raise LogError("ADMIN_CSRF", status=403)
            con.execute("UPDATE sessions SET last=? WHERE id=?", (now, row["id"]))
            return dict(row)

    def logout(self, session):
        with self.connect() as con:
            self.record(con, session, "session.logout", None, "success")
            con.execute("DELETE FROM sessions WHERE id=?", (session,))

    def intent(self, session, key, action, obj, payload):
        fingerprint = hashed(canonical(payload))
        with self.connect() as con:
            old = con.execute("SELECT * FROM actions WHERE id=?", (key,)).fetchone()
            if old:
                if (old["session"], old["action"], old["object"], old["payload_hash"]) != (
                    session,
                    action,
                    obj,
                    fingerprint,
                ):
                    raise LogError("ADMIN_ACTION_CONFLICT", status=409)
                return dict(old)
            self.record(con, session, action, obj, "submitted")
            con.execute(
                "INSERT INTO actions VALUES(?,?,?,?,?,?,?)",
                (key, session, action, obj, fingerprint, "submitted", time.time()),
            )
        return None

    def finish(self, session, key, action, obj, result):
        with self.connect() as con:
            con.execute("UPDATE actions SET result=? WHERE id=?", (result, key))
            self.record(con, session, action, obj, result)

    def audit(self, since, until, *, limit=100, cursor=None):
        if (
            not 0 <= since < until <= time.time() + 60
            or until - since > 86400
            or not 1 <= limit <= 500
        ):
            raise LogError("LOG_QUERY_WINDOW")
        values = [since, until]
        condition = ""
        if cursor:
            try:
                at, ident, start, end = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if (
                    len(cursor) > 512
                    or (start, end) != (since, until)
                    or not isinstance(at, (int, float))
                    or not re.fullmatch(r"[a-f0-9]{32}", ident)
                ):
                    raise ValueError()
            except Exception:
                raise LogError("LOG_CURSOR_INVALID") from None
            condition = " AND (at<? OR (at=? AND id<?))"
            values.extend([at, at, ident])
        with self.connect() as con:
            rows = con.execute(
                "SELECT id,at,action,object,result FROM audit WHERE at>=? AND at<?"
                + condition
                + " AND at>? ORDER BY at DESC,id DESC LIMIT ?",
                values + [time.time() - self.retention(con) * 86400, limit + 1],
            ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        cursor = (
            base64.urlsafe_b64encode(
                canonical([rows[-1]["at"], rows[-1]["id"], since, until]).encode()
            ).decode()
            if more
            else None
        )
        return {
            "items": [dict(row) | {"actor": "administrator", "origin": "admin"} for row in rows],
            "next_cursor": cursor,
            "as_of": time.time(),
            "truncated": False,
        }

    def note(self, session, action, obj=None, result="success"):
        with self.connect() as con:
            self.record(con, session, action, obj, result)

    def acknowledge(self, session, ident):
        with self.connect() as con:
            self.record(con, session, "alert.ack", ident, "success")
            con.execute(
                "INSERT OR REPLACE INTO acknowledgements VALUES(?,?,?)",
                (ident, time.time(), session),
            )
        return {"acknowledged": True, "business_recovery": False}

    def acknowledgements(self):
        with self.connect() as con:
            return {r["id"]: r["at"] for r in con.execute("SELECT id,at FROM acknowledgements")}

    @staticmethod
    def retention(con):
        return con.execute("SELECT value FROM settings WHERE key='audit_days'").fetchone()[0]

    def set_retention(self, days):
        if not isinstance(days, int) or not 1 <= days <= 365:
            raise LogError("ADMIN_INPUT_INVALID")
        with self.connect() as con:
            con.execute("UPDATE settings SET value=? WHERE key='audit_days'", (days,))

    def prune(self):
        now = time.time()
        with self.connect() as con:
            con.execute("DELETE FROM sessions WHERE last<? OR created<?", (now - 1800, now - 28800))
            con.execute("DELETE FROM codes WHERE expires<?", (now,))
            con.execute("DELETE FROM attempts WHERE at<?", (now - 60,))
            for table in ("audit", "actions", "acknowledgements"):
                con.execute(f"DELETE FROM {table} WHERE at<?", (now - self.retention(con) * 86400,))
            expired = con.execute(
                "SELECT id FROM exports WHERE expires<? "
                "OR session NOT IN (SELECT id FROM sessions)",
                (now,),
            ).fetchall()
            for row in expired:
                secure_path(self.root / "exports" / (row[0] + ".json")).unlink(missing_ok=True)
                con.execute("DELETE FROM exports WHERE id=?", (row[0],))

        # A crash between writing a file and committing its manifest must not leak storage.
        with self.connect() as con:
            live = {row[0] + ".json" for row in con.execute("SELECT id FROM exports")}
        for path in (self.root / "exports").iterdir():
            if path.name not in live and path.stat().st_mtime < now - 300:
                secure_path(path).unlink()
