"""Session-bound, bounded metadata exports; default per-bundle pseudonyms."""

import hashlib
import hmac
import secrets
import time
import uuid

from wayvia.observability.files import locked, write_json
from wayvia.observability.models import LogError, canonical
from wayvia.telemetry.store import secure_path

IDENTIFIERS = {
    "device_id",
    "subscription_id",
    "source_ip",
    "target_ip",
    "target_host",
    "account",
    "connection_id",
    "request_id",
    "object_id",
    "host_id",
    "host",
    "id",
    "source_epoch",
    "core_epoch",
    "boot_id",
    "generation",
    "revision",
    "record_id",
}


def redact(value, salt):
    if isinstance(value, list):
        return [redact(item, salt) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in IDENTIFIERS and item is not None:
            result[key] = (
                "anon-" + hmac.new(salt, str(item).encode(), hashlib.sha256).hexdigest()[:16]
            )
        elif key in {"source_port", "target_port"}:
            result[key] = item  # Protocol ports are useful; no endpoint address remains.
        else:
            result[key] = redact(item, salt)
    return result


def create(state, store, session, filters, mode, confirmed):
    if mode not in {"shared", "internal"} or (mode == "internal" and not confirmed):
        raise LogError("ADMIN_EXPORT_CONFIRM")
    now = time.time()
    filters = {"since": now - 3600, "until": now, **filters}
    state.prune()
    with locked(state.root / "export.lock", nonblocking=True):
        ident = uuid.uuid4().hex
        state.note(session, "export.create", ident, "submitted")
        rows = []
        cursor = None
        for _ in range(20):
            page = store.query(filters, cursor=cursor, limit=500)
            rows.extend(page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        truncated = bool(cursor)
        bundle = {
            "manifest": {
                "schema": 1,
                "created_at": now,
                "expires_at": now + 86400,
                "mode": mode,
                "records": len(rows),
                "truncated": truncated,
                "window": page["window"],
                "coverage": page["coverage"],
                "visibility": "connection_metadata_only; no_https_content",
            },
            "items": rows,
        }
        if mode == "shared":
            bundle = redact(bundle, secrets.token_bytes(32))
        bundle["manifest"]["content_sha256"] = hashlib.sha256(
            canonical(bundle["items"]).encode()
        ).hexdigest()
        bundle["manifest"]["consistency"] = (
            "bounded_paged_observations; concurrent_updates_may_move_records"
        )
        raw = (canonical(bundle) + "\n").encode()
        if len(raw) > 33554432:
            raise LogError("ADMIN_EXPORT_LIMIT")
        with state.connect() as con:
            used = sum(path.stat().st_size for path in (state.root / "exports").iterdir())
            if used + len(raw) > 134217728:
                raise LogError("ADMIN_EXPORT_CAPACITY", status=503)
            path = secure_path(state.root / "exports" / (ident + ".json"))
            try:
                write_json(path, bundle)
                con.execute(
                    "INSERT INTO exports VALUES(?,?,?,?,?,?,?)",
                    (
                        ident,
                        session,
                        now,
                        now + 86400,
                        len(raw),
                        hashlib.sha256(raw).hexdigest(),
                        mode,
                    ),
                )
                state.record(con, session, "export.create", ident, "success")
            except Exception:
                path.unlink(missing_ok=True)
                raise
        return {
            "id": ident,
            "mode": mode,
            "size": len(raw),
            "expires_at": now + 86400,
            "truncated": truncated,
        }


def lookup(state, session, ident):
    state.prune()
    with state.connect() as con:
        row = con.execute(
            "SELECT * FROM exports WHERE id=? AND session=? AND expires>?",
            (ident, session, time.time()),
        ).fetchone()
        if not row:
            raise LogError("ADMIN_EXPORT_NOT_FOUND", status=404)
        return dict(row)


def delete(state, session, ident):
    lookup(state, session, ident)
    with state.connect() as con:
        state.record(con, session, "export.delete", ident, "success")
        secure_path(state.root / "exports" / (ident + ".json")).unlink(missing_ok=True)
        con.execute("DELETE FROM exports WHERE id=?", (ident,))
    return {"deleted": True}
