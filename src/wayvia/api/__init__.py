"""Authenticated local management and separately publishable private subscriptions."""

import hashlib
import hmac
import json
import re
import uuid

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException

from wayvia.errors import ExitCode, WayviaError


class DeviceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    platform: str


class SubscriptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str
    format: str
    lifetime: int = 86400 * 30


class PublicationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exit_status: list[dict] | dict


def create_app(
    service, admin_token: str, *, subscriptions_only=False, log_sink=None, trusted_log_proxy=False
):
    if len(admin_token) < 32:
        raise WayviaError(
            "admin_token_invalid", "Administrator token requires at least 32 characters"
        )
    app = FastAPI(title="Wayvia", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = (
            getattr(request.state, "edge_request_id", None) or uuid.uuid4().hex
        )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(WayviaError)
    async def domain_error(request, exc):
        status = 401 if exc.exit_code == ExitCode.AUTH else 422
        if exc.code.endswith("not_found"):
            status = 404
        if exc.exit_code == ExitCode.RUNTIME:
            status = 503
        if exc.code == "idempotency_conflict":
            status = 409
        return JSONResponse(exc.as_dict(request.state.request_id), status_code=status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic errors contain original inputs; never return or log those.
        details = [{"path": list(error["loc"]), "code": error["type"]} for error in exc.errors()]
        return JSONResponse(
            {
                "code": "invalid_request",
                "message": "Request fields are invalid",
                "request_id": request.state.request_id,
                "details": details,
            },
            status_code=422,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(
            {
                "code": "http_error",
                "message": "Request could not be served",
                "request_id": request.state.request_id,
                "details": {"status": exc.status_code},
            },
            status_code=exc.status_code,
        )

    def authenticated(authorization: str | None = Header(default=None)):
        expected = "Bearer " + admin_token
        if authorization is None or not hmac.compare_digest(
            authorization.encode(), expected.encode()
        ):
            raise WayviaError(
                "administrator_denied",
                "Administrator authentication required",
                exit_code=ExitCode.AUTH,
            )

    def idempotent(scope, key, payload, action):
        if key is None or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise WayviaError("idempotency_required", "Management writes require Idempotency-Key")
        input_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        # Separate lock avoids nesting the credential change lock.
        import fcntl
        import time

        lock_path = service.root / "requests.lock"
        with lock_path.open("a") as stream:
            lock_path.chmod(0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            with service.db.connect() as connection:
                row = connection.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
            if row:
                if row["scope"] != scope or row["input_hash"] != input_hash:
                    raise WayviaError(
                        "idempotency_conflict", "Key was used for a different request"
                    )
                saved = json.loads(row["response"])
                if saved.get("status") == "in_progress":
                    raise WayviaError(
                        "request_recovery_required",
                        "Previous request was interrupted; inspect state "
                        "before retrying with a new key",
                        exit_code=ExitCode.RUNTIME,
                    )
                if "secret_ref" in saved:
                    return json.loads(service.secrets.read(saved["secret_ref"]))
                return saved
            # Reserve durably before side effects. A process crash cannot silently
            # issue another device/token when the same request is retried.
            with service.db.connect() as connection:
                connection.execute(
                    "INSERT INTO idempotency VALUES(?,?,?,?,?)",
                    (key, scope, input_hash, json.dumps({"status": "in_progress"}), time.time()),
                )
            try:
                result = action()
            except WayviaError:
                # Domain input/authentication errors occur before side effects.
                with service.db.connect() as connection:
                    connection.execute("DELETE FROM idempotency WHERE key=?", (key,))
                raise
            saved = result
            if "token" in result:
                ref = "request-" + uuid.uuid4().hex
                service.secrets.write(ref, json.dumps(result))
                saved = {"secret_ref": ref}
            with service.db.connect() as connection:
                connection.execute(
                    "UPDATE idempotency SET response=? WHERE key=?", (json.dumps(saved), key)
                )
            return result

    @app.get("/healthz")
    def health():
        return {"status": "alive", "dataplane": "unknown"}

    @app.get("/subscriptions/{token}/info")
    def subscription_info(token: str, request: Request):
        if log_sink:
            from wayvia.observability.http import identify

            return service.subscription_info(token, identity_sink=identify(request))
        return service.subscription_info(token)

    @app.get("/subscriptions/{token}/{format}")
    def fetch_subscription(
        request: Request, token: str, format: str, user_agent: str | None = Header(default=None)
    ):
        kwargs = {}
        if log_sink:
            from wayvia.observability.http import identify

            kwargs["identity_sink"] = identify(request)
        document = service.fetch_document(token, format, client_claim=user_agent, **kwargs)
        if format == "mihomo" and request.url.scheme == "https":
            document["headers"]["profile-web-page-url"] = str(
                request.url.replace(
                    path=request.url.path.rsplit("/", 1)[0] + "/info", query="", fragment=""
                )
            )
        media = "application/json" if format == "sing-box" else "application/yaml"
        return Response(document["text"], media_type=media, headers=document["headers"])

    if log_sink:
        from wayvia.observability.http import install_middleware

        install_middleware(app, log_sink, trusted_log_proxy)

    if subscriptions_only:
        return app

    admin = [Depends(authenticated)]

    @app.get("/api/v1/devices/{device_id}/history", dependencies=admin)
    def usage_history(device_id: str, since: float, until: float, grain: str = "minute"):
        from wayvia.services.usage_history import history

        return history(service.db, device_id, start=since, end=until, grain=grain)

    @app.get("/api/v1/devices/{device_id}/history/export", dependencies=admin)
    def export_usage_history(device_id: str, since: float, until: float, grain: str = "minute"):
        result = usage_history(device_id, since, until, grain)
        return JSONResponse(
            result,
            headers={
                "Content-Disposition": 'attachment; filename="usage.json"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/v1/config/publication", dependencies=admin)
    def publication():
        return service.publication()

    @app.post("/api/v1/config/publication", dependencies=admin)
    def publish_configuration(
        data: PublicationInput, idempotency_key: str | None = Header(default=None)
    ):
        return idempotent(
            "configuration.publish",
            idempotency_key,
            data.model_dump(),
            lambda: service.publish(data.exit_status),
        )

    @app.get("/api/v1/status", dependencies=admin)
    def status():
        return service.status()

    @app.get("/api/v1/devices", dependencies=admin)
    def devices():
        return {"devices": service.devices()}

    @app.get("/api/v1/subscriptions", dependencies=admin)
    def subscriptions():
        return {"subscriptions": service.subscriptions()}

    @app.post("/api/v1/devices", dependencies=admin)
    def add_device(data: DeviceInput, idempotency_key: str | None = Header(default=None)):
        return idempotent(
            "device.add",
            idempotency_key,
            data.model_dump(),
            lambda: service.add(data.name, data.platform),
        )

    @app.post("/api/v1/devices/{device_id}/rotate", dependencies=admin)
    def rotate_device(device_id: str, idempotency_key: str | None = Header(default=None)):
        return idempotent(
            "device.rotate", idempotency_key, {"id": device_id}, lambda: service.rotate(device_id)
        )

    @app.post("/api/v1/devices/{device_id}/revoke", dependencies=admin)
    def revoke_device(device_id: str, idempotency_key: str | None = Header(default=None)):
        return idempotent(
            "device.revoke",
            idempotency_key,
            {"id": device_id},
            lambda: service.revoke_device(device_id),
        )

    @app.post("/api/v1/subscriptions", dependencies=admin)
    def issue_subscription(
        data: SubscriptionInput, idempotency_key: str | None = Header(default=None)
    ):
        return idempotent(
            "subscription.issue",
            idempotency_key,
            data.model_dump(),
            lambda: service.issue(data.device_id, data.format, data.lifetime),
        )

    @app.delete("/api/v1/subscriptions/{subscription_id}", dependencies=admin)
    def revoke_subscription(
        subscription_id: str, idempotency_key: str | None = Header(default=None)
    ):
        return idempotent(
            "subscription.revoke",
            idempotency_key,
            {"id": subscription_id},
            lambda: service.revoke_subscription(subscription_id),
        )

    return app
