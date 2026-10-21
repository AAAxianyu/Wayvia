"""Durable best-effort payload accounting; stale telemetry is never 'offline'."""

import json
import time

FRESH_SECONDS = 20


class UsageStore:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _state(connection):
        row = connection.execute("SELECT value FROM settings WHERE key='usage_state'").fetchone()
        return json.loads(row[0]) if row else {}

    @staticmethod
    def _save(connection, state):
        connection.execute(
            "INSERT INTO settings VALUES('usage_state',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(state),),
        )

    def failure(self, code):
        with self.db.change_lock(), self.db.connect() as connection:
            state = self._state(connection)
            state.update(status="unavailable", error=code, attempted_at=time.time())
            self._save(connection, state)

    def record(self, sample):
        observed = sample["observed_at"]
        with self.db.change_lock(), self.db.connect() as connection:
            state = self._state(connection)
            last = state.get("observed_at")
            if last is not None and observed <= last:
                return  # An older concurrent collector must not rewind watermarks.
            gaps = state.get("gaps", 0)
            reason = None
            if state.get("generation") != sample["generation"]:
                connection.execute("DELETE FROM usage_connections")
                if last is not None:
                    gaps += 1
                    reason = "core_generation_changed"
            elif sample["closed_buffer_full"] and sample["oldest_closed_at"] > (last or 0):
                gaps += 1
                reason = "closed_history_overflow"
            known = {
                r["id"]: r["status"] for r in connection.execute("SELECT id,status FROM devices")
            }
            for device in known:
                connection.execute(
                    "INSERT OR IGNORE INTO device_usage(device_id,started_at) VALUES(?,?)",
                    (device, observed),
                )
            previous = {
                r["id"]: dict(r) for r in connection.execute("SELECT * FROM usage_connections")
            }
            active = dict.fromkeys(known, 0)
            seen = set()
            for item in sample["connections"]:
                device = item["device_id"]
                if device not in known:
                    continue
                ident = item["id"]
                seen.add(ident)
                old = previous.get(ident)
                if old and old["device_id"] != device:
                    raise ValueError("usage identity changed")
                upload = max(item["upload"], old["upload_bytes"] if old else 0)
                download = max(item["download"], old["download_bytes"] if old else 0)
                up_delta = upload - (old["upload_bytes"] if old else 0)
                down_delta = download - (old["download_bytes"] if old else 0)
                if item["closed_at"] is None and known[device] == "active":
                    active[device] += 1
                used_at = None
                if up_delta or down_delta:
                    used_at = min(item["closed_at"] or observed, observed)
                elif old is None:
                    used_at = min(item["created_at"], observed)
                connection.execute(
                    "UPDATE device_usage SET upload_bytes=upload_bytes+?, "
                    "download_bytes=download_bytes+?,last_used_at=CASE "
                    "WHEN ? IS NULL THEN last_used_at ELSE max(coalesce(last_used_at,0),?) END "
                    "WHERE device_id=?",
                    (up_delta, down_delta, used_at, used_at, device),
                )
                connection.execute(
                    "INSERT INTO usage_connections VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE "
                    "SET upload_bytes=excluded.upload_bytes,download_bytes=excluded.download_bytes,"
                    "closed_at=excluded.closed_at,observed_at=excluded.observed_at",
                    (ident, device, upload, download, item["closed_at"], observed),
                )
            for ident, old in previous.items():
                if ident not in seen:
                    # Closed history cannot reappear after eviction. Missing active
                    # connections mean their final bytes may have escaped observation.
                    if old["closed_at"] is None:
                        gaps += 1
                        reason = "connection_final_counter_missing"
                    connection.execute("DELETE FROM usage_connections WHERE id=?", (ident,))
            for device, count in active.items():
                connection.execute(
                    "UPDATE device_usage SET active_connections=?,observed_at=? WHERE device_id=?",
                    (count, observed, device),
                )
            state.update(
                status="ok",
                error=None,
                attempted_at=observed,
                observed_at=observed,
                generation=sample["generation"],
                gaps=gaps,
            )
            if reason:
                state.update(last_gap_reason=reason, last_gap_at=observed)
            self._save(connection, state)

    def device(self, device_id, *, now=None):
        now = time.time() if now is None else now
        with self.db.connect() as connection:
            state = self._state(connection)
            row = connection.execute(
                "SELECT * FROM device_usage WHERE device_id=?", (device_id,)
            ).fetchone()
            fetched = connection.execute(
                "SELECT max(last_fetched_at),coalesce(sum(fetch_count),0) "
                "FROM subscriptions WHERE device_id=?",
                (device_id,),
            ).fetchone()
        fresh = bool(
            row
            and state.get("status") == "ok"
            and row["observed_at"] is not None
            and 0 <= now - row["observed_at"] <= FRESH_SECONDS
        )
        return {
            "online": bool(row["active_connections"]) if fresh else None,
            "status": ("online" if row["active_connections"] else "offline")
            if fresh
            else "unknown",
            "active_connections": row["active_connections"] if fresh else None,
            "last_used_at": row["last_used_at"] if row else None,
            "upload_bytes": row["upload_bytes"] if row else 0,
            "download_bytes": row["download_bytes"] if row else 0,
            "total_bytes": row["upload_bytes"] + row["download_bytes"] if row else 0,
            "observed_at": row["observed_at"] if row else None,
            "counting_since": row["started_at"] if row else None,
            "last_subscription_fetch_at": fetched[0],
            "subscription_fetches": fetched[1],
            "accounting": "best_effort_proxy_payload",
            "gaps": state.get("gaps", 0),
            "last_gap_reason": state.get("last_gap_reason"),
            "collection_error": state.get("error"),
        }
