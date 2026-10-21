"""SQLite metadata, explicit migrations, and cross-process change locks."""

import fcntl
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from wayvia.errors import WayviaError

MIGRATIONS = {
    1: """
    CREATE TABLE devices (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, platform TEXT NOT NULL,
      status TEXT NOT NULL, credential_version INTEGER NOT NULL,
      secret_ref TEXT NOT NULL, desired_action TEXT, revision INTEGER NOT NULL,
      created_at REAL NOT NULL, updated_at REAL NOT NULL, evidence TEXT NOT NULL
    );
    CREATE TABLE subscriptions (
      id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id),
      token_hash TEXT NOT NULL UNIQUE, format TEXT NOT NULL, expires_at REAL NOT NULL,
      revoked_at REAL, created_at REAL NOT NULL
    );
    CREATE TABLE audit (
      id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
      object_id TEXT NOT NULL, result TEXT NOT NULL, created_at REAL NOT NULL
    );
    CREATE TABLE idempotency (
      key TEXT PRIMARY KEY, scope TEXT NOT NULL, input_hash TEXT NOT NULL,
      response TEXT NOT NULL, created_at REAL NOT NULL
    );
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    2: """
    CREATE TABLE config_publications (
      revision TEXT PRIMARY KEY, config_json TEXT NOT NULL,
      confirmations_json TEXT NOT NULL, published_at REAL NOT NULL
    );
    """,
    3: """
    ALTER TABLE subscriptions ADD COLUMN last_fetched_at REAL;
    ALTER TABLE subscriptions ADD COLUMN fetch_count INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE device_usage (
      device_id TEXT PRIMARY KEY REFERENCES devices(id),
      upload_bytes INTEGER NOT NULL DEFAULT 0, download_bytes INTEGER NOT NULL DEFAULT 0,
      active_connections INTEGER NOT NULL DEFAULT 0, last_used_at REAL,
      observed_at REAL, started_at REAL NOT NULL
    );
    CREATE TABLE usage_connections (
      id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id),
      upload_bytes INTEGER NOT NULL, download_bytes INTEGER NOT NULL,
      closed_at REAL, observed_at REAL NOT NULL
    );
    """,
}


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_path = self.path.with_suffix(".lock")
        with self.change_lock():
            self.migrate()
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self):
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"
            )
            versions = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            }
            if versions - MIGRATIONS.keys():
                raise WayviaError("database_version", "Database was created by a newer Wayvia")
            for version, script in MIGRATIONS.items():
                if version not in versions:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        + script
                        + f"INSERT INTO schema_migrations VALUES ({version});\nCOMMIT;"
                    )

    @contextmanager
    def change_lock(self):
        with self.lock_path.open("a") as stream:
            self.lock_path.chmod(0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
