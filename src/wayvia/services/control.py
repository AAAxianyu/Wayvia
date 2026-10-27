"""Persistent devices, independent credentials, and private subscriptions."""

import hashlib
import json
import secrets
import time
import uuid
from pathlib import Path

from wayvia.errors import ExitCode, WayviaError
from wayvia.secrets import SecretStore
from wayvia.storage import Database


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AgentDataplane:
    simulated = False

    def __init__(self, socket_path: Path):
        self.socket_path = socket_path

    def sync(self, devices: list[dict], revision: int) -> dict:
        from wayvia.agent.client import AgentClient

        response = AgentClient(self.socket_path).request(
            {"operation": "sync_devices", "devices": devices, "revision": revision}
        )
        if response.get("status") not in {"active", "applied"}:
            raise WayviaError(
                "dataplane_apply_failed",
                "Ingress did not accept credential revision",
                exit_code=ExitCode.RUNTIME,
            )
        return {
            "revision": revision,
            "status": "active",
            "simulated": False,
            "sessions": "all_previous_sessions_closed",
        }

    def status(self):
        from wayvia.agent.client import AgentClient

        return AgentClient(self.socket_path).request({"operation": "status"})

    def usage(self):
        from wayvia.agent.client import AgentClient

        return AgentClient(self.socket_path, timeout=5).request({"operation": "device_usage"})

    def block(self):
        from wayvia.agent.client import AgentClient

        return AgentClient(self.socket_path).request({"operation": "block_ingress"})


class SimulatedDataplane:
    """Only explicitly selected in tests/demo; never selected by production fallback."""

    simulated = True

    def __init__(self):
        self.accepted: dict[str, str] = {}

    def sync(self, devices: list[dict], revision: int) -> dict:
        self.accepted = {device["id"]: device["password"] for device in devices}
        return {"revision": revision, "status": "active", "simulated": True}

    def block(self):
        self.accepted.clear()
        return {"status": "blocked", "simulated": True}


class ControlService:
    def __init__(self, root: Path, config, *, mode="production", dataplane=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config = config
        self.db = Database(self.root / "metadata.db")
        self.secrets = SecretStore(self.root / "secrets")
        self.mode = mode
        self.dataplane = dataplane or AgentDataplane(Path("/run/wayvia/agent.sock"))
        if mode not in {"production", "demo", "test"}:
            raise WayviaError("invalid_mode", "Unknown execution mode")
        if mode == "production" and self.dataplane.simulated:
            raise WayviaError("fake_forbidden", "Production requires a real dataplane")
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO settings(key,value) VALUES('desired_revision',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (config.revision,),
            )

    def _audit(self, connection, action, object_id, result):
        connection.execute(
            "INSERT INTO audit(actor,action,object_id,result,created_at) VALUES(?,?,?,?,?)",
            ("local-admin", action, object_id, result, time.time()),
        )

    def _public(self, row):
        from wayvia.services.usage import UsageStore

        return {
            key: row[key]
            for key in (
                "id",
                "name",
                "platform",
                "status",
                "credential_version",
                "revision",
                "created_at",
                "updated_at",
                "desired_action",
            )
        } | {
            "evidence": json.loads(row["evidence"]),
            "simulated": self.dataplane.simulated,
            "usage": UsageStore(self.db).device(row["id"]),
        }

    def collect_usage(self):
        from wayvia.services.usage import UsageStore

        store = UsageStore(self.db)
        try:
            if self.dataplane.simulated:
                raise WayviaError("USAGE_SIMULATED", "Simulation cannot observe real device usage")
            sample = self.dataplane.usage()
            store.record(sample)
        except (WayviaError, OSError, ValueError) as exc:
            code = exc.code if isinstance(exc, WayviaError) else "USAGE_COLLECTION_FAILED"
            store.failure(code)
            return {"status": "unavailable", "code": code}
        return {"status": "observed", "observed_at": sample["observed_at"]}

    def device(self, device_id: str) -> dict:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if row is None:
                raise WayviaError("device_not_found", "Device does not exist")
            return self._public(row)

    def devices(self) -> list[dict]:
        with self.db.connect() as connection:
            return [
                self._public(row)
                for row in connection.execute("SELECT * FROM devices ORDER BY created_at")
            ]

    def status(self):
        result = {
            "mode": self.mode,
            "devices": self.devices(),
            "configuration_revision": self.config.revision,
            "publication": self.publication(),
        }
        if self.dataplane.simulated:
            result["dataplane"] = {"status": "simulated"}
        else:
            try:
                result["dataplane"] = self.dataplane.status()
            except (OSError, WayviaError, AttributeError):
                result["dataplane"] = {"status": "unknown", "code": "agent_unavailable"}
        return result

    def publication(self):
        """Public metadata only; the durable snapshot includes server secret refs."""
        with self.db.connect() as connection:
            desired = connection.execute(
                "SELECT value FROM settings WHERE key='desired_revision'"
            ).fetchone()
            row = connection.execute(
                "SELECT p.* FROM config_publications p JOIN settings s "
                "ON s.key='published_revision' AND s.value=p.revision"
            ).fetchone()
        result = {
            "desired_revision": desired[0] if desired else self.config.revision,
            "published_revision": row["revision"] if row else None,
            "status": "published" if row else "unpublished",
        }
        if row:
            result["published_at"] = row["published_at"]
            result["confirmations"] = {
                role: {key: receipt.get(key) for key in ("id", "revision", "generation")}
                for role, receipt in json.loads(row["confirmations_json"]).items()
            }
        return result

    @staticmethod
    def _confirmation(status, role, revision):
        from wayvia.services.diagnostics import transaction_diagnostic

        if isinstance(status, dict):
            status = status.get("agent", status.get("dataplane"))
        if not isinstance(status, list):
            raise WayviaError("confirmation_invalid", "Expected an exported agent status list")
        current = [
            tx
            for tx in status
            if isinstance(tx, dict) and tx.get("role") == role and tx.get("current") is True
        ]
        if len(current) != 1 or current[0].get("revision") != revision:
            raise WayviaError(
                "configuration_unconfirmed",
                "Both roles must confirm this exact configuration revision",
                details={"role": role},
                exit_code=ExitCode.RUNTIME,
            )
        receipt = current[0]
        diagnostic = transaction_diagnostic(receipt)
        if diagnostic["status"] != ("healthy" if role == "ingress" else "ready"):
            raise WayviaError(
                "configuration_unconfirmed",
                "Publication requires current, healthy and fresh confirmation evidence",
                details={"role": role, "reason": diagnostic.get("error", "unconfirmed")},
                exit_code=ExitCode.RUNTIME,
            )
        return receipt

    def publish(self, exit_status):
        """Explicit operator publication after independent exit/ingress confirmation.

        The remote receipt is imported by the authenticated administrator over
        their trusted management channel. It is not a cross-host transaction or
        a claim that an arbitrary supplied JSON document authenticates a server.
        """
        if self.dataplane.simulated:
            raise WayviaError("fake_forbidden", "Publication requires real agent evidence")
        with self.db.change_lock():
            revision = self.config.revision
            receipts = {
                "exit": self._confirmation(exit_status, "exit", revision),
                "ingress": self._confirmation(self.dataplane.status(), "ingress", revision),
            }
            config_json = self.config.model_dump_json()
            # Do not let validation/serialization consume the evidence lifetime.
            for role, receipt in receipts.items():
                self._confirmation([receipt], role, revision)
            with self.db.connect() as connection:
                connection.execute(
                    "INSERT INTO config_publications VALUES(?,?,?,?) "
                    "ON CONFLICT(revision) DO UPDATE SET "
                    "confirmations_json=excluded.confirmations_json,published_at=excluded.published_at",
                    (revision, config_json, json.dumps(receipts), time.time()),
                )
                connection.execute(
                    "INSERT INTO settings(key,value) VALUES('published_revision',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (revision,),
                )
                self._audit(connection, "configuration.publish", revision, "published")
        return self.publication()

    def _published_config(self):
        from wayvia.config import WayviaConfig

        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT p.* FROM config_publications p JOIN settings s "
                "ON s.key='published_revision' AND s.value=p.revision"
            ).fetchone()
        if row is None:
            if self.mode in {"test", "demo"}:
                return self.config
            raise WayviaError(
                "configuration_unpublished",
                "Confirm both roles and explicitly publish a configuration before exporting",
                exit_code=ExitCode.RUNTIME,
            )
        config = WayviaConfig.model_validate_json(row["config_json"])
        if config.revision != row["revision"]:
            raise WayviaError(
                "publication_corrupt",
                "Published configuration digest is invalid",
                exit_code=ExitCode.RUNTIME,
            )
        return config

    def _sync(self) -> dict:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM devices WHERE desired_action IS NOT 'revoke' AND status != 'revoked'"
            ).fetchall()
            revision = connection.execute(
                "SELECT coalesce(max(revision),0) FROM devices"
            ).fetchone()[0]
        try:
            accepted = [
                {"id": row["id"], "password": self.secrets.read(row["secret_ref"])} for row in rows
            ]
            evidence = self.dataplane.sync(accepted, revision)
            if evidence.get("status") != "active":
                raise WayviaError("dataplane_apply_failed", "Ingress revision not accepted")
        except Exception as error:
            not_deployed = isinstance(error, WayviaError) and error.code == "INGRESS_NOT_DEPLOYED"
            if not_deployed:
                # The agent has proved no deployment or ingress artifact exists.
                # Keep its durable desired-credential checkpoint, without turning
                # first-time pending creation into an explicit deployment block.
                protection = {"status": "not_deployed", "code": "INGRESS_NOT_DEPLOYED"}
            else:
                try:
                    protection = self.dataplane.block()
                except Exception:
                    protection = {"status": "unknown", "code": "ingress_block_unconfirmed"}
            failure = {
                "status": "unconfirmed",
                "code": "INGRESS_NOT_DEPLOYED" if not_deployed else "dataplane_apply_failed",
                "protection": protection,
            }
            with self.db.connect() as connection:
                connection.execute(
                    "UPDATE devices SET status=CASE WHEN desired_action='add' "
                    "THEN 'pending' ELSE 'degraded' END, evidence=?,updated_at=? "
                    "WHERE status!='revoked'",
                    (
                        json.dumps(failure),
                        time.time(),
                    ),
                )
            return failure
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE devices SET status=CASE WHEN desired_action='revoke' "
                "THEN 'revoked' ELSE 'active' END,desired_action=NULL,evidence=?, "
                "updated_at=? WHERE status!='revoked'",
                (json.dumps(evidence), time.time()),
            )
        return evidence

    def add(self, name: str, platform: str) -> dict:
        if (
            not name.strip()
            or len(name) > 100
            or platform not in {"android", "ios", "desktop", "gateway"}
        ):
            raise WayviaError("invalid_device", "Name or platform is invalid")
        with self.db.change_lock():
            device_id = uuid.uuid4().hex
            secret_ref = f"device-{device_id}-v1"
            self.secrets.write(secret_ref, secrets.token_urlsafe(32))
            now = time.time()
            with self.db.connect() as connection:
                revision = connection.execute(
                    "SELECT coalesce(max(revision),0)+1 FROM devices"
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO devices VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        device_id,
                        name,
                        platform,
                        "pending",
                        1,
                        secret_ref,
                        "add",
                        revision,
                        now,
                        now,
                        "{}",
                    ),
                )
                self._audit(connection, "device.add", device_id, "pending")
            self._sync()
            return self.device(device_id)

    def rotate(self, device_id: str) -> dict:
        with self.db.change_lock():
            device = self.device(device_id)
            if device["status"] == "revoked" or device["desired_action"] == "revoke":
                raise WayviaError("device_revoked", "Revoked device cannot rotate")
            version = device["credential_version"] + 1
            # An interrupted write may leave an unreferenced secret behind.
            # Never re-use its name: only the reference committed in SQLite is
            # authoritative. Reconcile resumes a committed rotation unchanged.
            ref = f"device-{device_id}-v{version}-{uuid.uuid4().hex}"
            self.secrets.write(ref, secrets.token_urlsafe(32))
            with self.db.connect() as connection:
                revision = connection.execute("SELECT max(revision)+1 FROM devices").fetchone()[0]
                connection.execute(
                    "UPDATE devices SET credential_version=?,secret_ref=?,status='pending', "
                    "desired_action='rotate',revision=?,updated_at=? WHERE id=?",
                    (version, ref, revision, time.time(), device_id),
                )
                self._audit(connection, "device.rotate", device_id, "pending")
            self._sync()
            return self.device(device_id)

    def revoke_device(self, device_id: str) -> dict:
        with self.db.change_lock():
            self.device(device_id)
            with self.db.connect() as connection:
                revision = connection.execute("SELECT max(revision)+1 FROM devices").fetchone()[0]
                connection.execute(
                    "UPDATE devices SET desired_action='revoke',status='degraded', "
                    "revision=?,updated_at=? WHERE id=?",
                    (revision, time.time(), device_id),
                )
                connection.execute(
                    "UPDATE subscriptions SET revoked_at=? WHERE device_id=?",
                    (time.time(), device_id),
                )
                self._audit(connection, "device.revoke", device_id, "pending")
            self._sync()
            return self.device(device_id)

    def reconcile(self) -> dict:
        with self.db.change_lock():
            return self._sync()

    def export(self, device_id: str, format: str) -> str:
        from wayvia.render import ProfileRenderer

        device = self.device(device_id)
        if device["status"] != "active" or device["desired_action"]:
            raise WayviaError("device_inactive", "Device credentials are not confirmed active")
        with self.db.connect() as connection:
            ref = connection.execute(
                "SELECT secret_ref FROM devices WHERE id=?", (device_id,)
            ).fetchone()[0]
        return ProfileRenderer(self._published_config()).render(
            device, self.secrets.read(ref), format
        )

    def issue(self, device_id: str, format: str, lifetime: int = 86400 * 30) -> dict:
        self.export(device_id, format)
        if not 60 <= lifetime <= 86400 * 365:
            raise WayviaError("invalid_expiry", "Subscription lifetime outside supported range")
        token = secrets.token_urlsafe(48)
        subscription_id = uuid.uuid4().hex
        expires = time.time() + lifetime
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO subscriptions "
                "(id,device_id,token_hash,format,expires_at,revoked_at,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (subscription_id, device_id, digest(token), format, expires, None, time.time()),
            )
            self._audit(connection, "subscription.issue", subscription_id, "issued")
        return {
            "id": subscription_id,
            "token": token,
            "format": format,
            "expires_at": expires,
            "path": f"/subscriptions/{token}/{format}",
        }

    def fetch(self, token: str, format: str, *, client_claim: str | None = None) -> str:
        return self.fetch_document(token, format, client_claim=client_claim)["text"]

    def _subscription(self, token, format=None):
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE token_hash=?", (digest(token),)
            ).fetchone()
        if (
            row is None
            or row["revoked_at"] is not None
            or row["expires_at"] <= time.time()
            or (format is not None and row["format"] != format)
        ):
            raise WayviaError(
                "subscription_denied", "Subscription is unavailable", exit_code=ExitCode.AUTH
            )
        return row

    def subscription_info(self, token, *, identity_sink=None):
        from wayvia.client import contract, title
        from wayvia.client.names import display_name

        row = self._subscription(token)
        device = self.device(row["device_id"])
        if device["status"] != "active" or device["desired_action"]:
            raise WayviaError(
                "subscription_denied", "Subscription is unavailable", exit_code=ExitCode.AUTH
            )
        self._published_config()
        if identity_sink:
            identity_sink(row["device_id"], row["id"])
        return {
            "schema_version": 1,
            "title": title(device["name"], display_name(self.db, row["id"])),
            "format": row["format"],
            "download_link_expires_at": row["expires_at"],
            "device_status": device["status"],
            "usage": device["usage"],
            "client": contract(),
        }

    def fetch_document(self, token, format, *, client_claim=None, identity_sink=None):
        from wayvia.client import headers
        from wayvia.client.names import display_name

        row = self._subscription(token, format)
        text = self.export(row["device_id"], format)
        response_headers = headers(
            self.device(row["device_id"])["name"], format, display_name(self.db, row["id"])
        )
        with self.db.change_lock(), self.db.connect() as connection:
            from wayvia.services.usage_history import fetched

            now = time.time()
            cursor = connection.execute(
                "UPDATE subscriptions SET last_fetched_at=?,fetch_count=fetch_count+1 "
                "WHERE id=? AND revoked_at IS NULL AND expires_at>?",
                (now, row["id"], now),
            )
            if cursor.rowcount != 1:
                raise WayviaError(
                    "subscription_denied", "Subscription is unavailable", exit_code=ExitCode.AUTH
                )
            fetched(connection, row["device_id"], now, client_claim)
        if identity_sink:
            identity_sink(row["device_id"], row["id"])
        return {"text": text, "headers": response_headers}

    def subscriptions(self):
        with self.db.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT id,device_id,format,expires_at,revoked_at,created_at,last_fetched_at,"
                    "fetch_count FROM subscriptions ORDER BY created_at"
                )
            ]

    def revoke_subscription(self, subscription_id: str) -> dict:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT id FROM subscriptions WHERE id=?", (subscription_id,)
            ).fetchone()
            if row is None:
                raise WayviaError("subscription_not_found", "Subscription does not exist")
            connection.execute(
                "UPDATE subscriptions SET revoked_at=? WHERE id=?", (time.time(), subscription_id)
            )
            self._audit(connection, "subscription.revoke", subscription_id, "revoked")
        return {"id": subscription_id, "status": "revoked", "device_permission": "unchanged"}
