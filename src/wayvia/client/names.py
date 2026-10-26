"""Subscription presentation metadata; no credential, device revision or network writes."""

import time

from wayvia.client import clean_name
from wayvia.errors import WayviaError


def display_name(db, subscription_id):
    with db.connect() as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key=?", ("subscription_title:" + subscription_id,)
        ).fetchone()
    return row[0] if row else None


def set_display_name(db, subscription_id, name):
    value = clean_name(name)
    if not value:
        raise WayviaError("subscription_name_invalid", "A visible subscription name is required")
    with db.change_lock(), db.connect() as connection:
        row = connection.execute(
            "SELECT id FROM subscriptions WHERE id=?", (subscription_id,)
        ).fetchone()
        if row is None:
            raise WayviaError("subscription_not_found", "Subscription does not exist")
        connection.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("subscription_title:" + subscription_id, value),
        )
        connection.execute(
            "INSERT INTO audit(actor,action,object_id,result,created_at) VALUES(?,?,?,?,?)",
            ("local-admin", "subscription.name", subscription_id, "updated", time.time()),
        )
    return {"id": subscription_id, "display_name": value, "device_permission": "unchanged"}
