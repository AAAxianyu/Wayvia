"""Private loopback app. No wildcard CORS, bearer auth, third-party assets or arbitrary RPC."""

import hashlib
import hmac
import re
import threading
import time
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException

from wayvia.admin import exports
from wayvia.admin.state import hashed
from wayvia.observability.models import LogError, canonical

STATIC = Path(__file__).parent / "static"
ID = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    code: Annotated[str, Field(min_length=16, max_length=128)]


class Query(Input):
    filters: dict = {}
    cursor: Annotated[str, Field(max_length=512)] | None = None
    limit: Annotated[int, Field(ge=1, le=500)] = 100


class Action(Input):
    action: Literal[
        "device.add", "device.rotate", "device.revoke", "subscription.issue", "subscription.revoke"
    ]
    object_id: ID | None = None
    key: ID
    confirmed: bool = False
    payload: dict = {}


class PolicyChange(Input):
    expected_revision: int
    changes: dict
    key: ID
    confirmed: bool = False


class Export(Input):
    filters: dict = {}
    mode: Literal["shared", "internal"] = "shared"
    confirmed: bool = False


def csrf_for(token, ident):
    return hmac.new(token.encode(), ident.encode(), hashlib.sha256).hexdigest()


def create_app(state, store, control, *, origin="http://127.0.0.1:8790"):
    from urllib.parse import urlsplit

    url = urlsplit(origin)
    if (
        url.hostname not in {"127.0.0.1", "localhost", "::1"}
        or url.scheme not in {"http", "https"}
        or url.path
        or url.query
        or url.fragment
        or url.username
    ):
        raise LogError("ADMIN_PRIVATE_ORIGIN")
    secure = url.scheme == "https"
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        stop = threading.Event()

        def cleanup_loop():
            while not stop.wait(60):
                try:
                    state.prune()
                except Exception:
                    pass

        worker = threading.Thread(target=cleanup_loop, daemon=True)
        worker.start()
        yield
        stop.set()
        worker.join(timeout=3)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    queries = threading.BoundedSemaphore(2)
    cleanup = [0.0]

    def error(code, status):
        return JSONResponse(
            {"code": code, "message": "操作未完成，请检查登录状态或观测来源后重试。"},
            status_code=status,
        )

    @app.middleware("http")
    async def boundary(request, call_next):
        if (
            request.headers.get("host") != url.netloc
            or request.client is None
            or request.client.host not in {"127.0.0.1", "::1", "testclient"}
        ):
            return error("ADMIN_PRIVATE_ONLY", 403)
        if request.method not in {"GET", "POST", "DELETE"}:
            return error("ADMIN_METHOD", 405)
        if request.method != "GET" and request.headers.get("origin") != origin:
            return error("ADMIN_ORIGIN", 403)
        if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
            return error("ADMIN_ORIGIN", 403)
        try:
            if request.method != "GET":
                if request.headers.get("content-type", "").split(";")[0] != "application/json":
                    return error("ADMIN_JSON_REQUIRED", 415)
                body = bytearray()
                async for block in request.stream():
                    body.extend(block)
                    if len(body) > 16384:
                        return error("ADMIN_BODY_LIMIT", 413)
                request._body = bytes(body)
            response = await call_next(request)
        except Exception:
            response = error("ADMIN_UNAVAILABLE", 503)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'self'; style-src 'self'; "
                    "img-src 'self'; connect-src 'self'; base-uri 'none'; "
                    "frame-ancestors 'none'; form-action 'self'"
                ),
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "X-Frame-Options": "DENY",
            }
        )
        return response

    @app.exception_handler(LogError)
    async def log_error(request, exc):
        return error(exc.code, exc.status)

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return error("ADMIN_INPUT_INVALID", 422)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error("ADMIN_INPUT_INVALID", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error("ADMIN_NOT_FOUND", exc.status_code)

    def authenticated(request: Request):
        token = request.cookies.get("wayvia_admin")
        row = state.session(
            token, request.headers.get("x-csrf-token"), write=request.method != "GET"
        )
        if time.monotonic() - cleanup[0] > 60:
            state.prune()
            cleanup[0] = time.monotonic()
        return row

    def bounded():
        if not queries.acquire(blocking=False):
            raise LogError("ADMIN_QUERY_BUSY", status=429)
        try:
            yield
        finally:
            queries.release()

    @app.get("/admin")
    @app.get("/admin/")
    def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/admin/assets/{name}")
    def asset(name: str):
        if name not in {"app.js", "app.css"}:
            raise LogError("ADMIN_NOT_FOUND", status=404)
        return FileResponse(
            STATIC / name, media_type="text/javascript" if name.endswith(".js") else "text/css"
        )

    @app.post("/admin/session")
    def login(data: Login):
        token, _, ident = state.login(data.code)
        csrf = csrf_for(token, ident)
        with state.connect() as con:
            con.execute("UPDATE sessions SET csrf=? WHERE id=?", (hashed(csrf), ident))
        response = JSONResponse({"authenticated": True, "csrf": csrf, "expires_in": 1800})
        response.set_cookie(
            "wayvia_admin",
            token,
            max_age=28800,
            httponly=True,
            samesite="strict",
            secure=secure,
            path="/admin",
        )
        return response

    @app.get("/admin/session")
    def session(request: Request, auth=Depends(authenticated)):
        return {
            "authenticated": True,
            "csrf": csrf_for(request.cookies["wayvia_admin"], auth["id"]),
            "expires_in": max(0, min(1800, auth["created"] + 28800 - time.time())),
        }

    @app.delete("/admin/session")
    def logout(auth=Depends(authenticated)):
        state.logout(auth["id"])
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(
            "wayvia_admin", path="/admin", secure=secure, httponly=True, samesite="strict"
        )
        return response

    @app.get("/admin/api/overview", dependencies=[Depends(bounded)])
    def overview(auth=Depends(authenticated)):
        try:
            status = control.read("status")
        except LogError:
            status = {"dataplane": {"status": "unknown"}, "devices": []}
        try:
            sources = store.sources()
            config = store.policy()
        except LogError:
            sources = [{"host": "unknown", "source": "collector", "state": "unavailable"}]
            config = None
        from wayvia.services.diagnostics import transaction_diagnostic

        current = status.get("dataplane")
        health = (
            [
                transaction_diagnostic(tx)
                for tx in current
                if isinstance(tx, dict) and tx.get("current")
            ]
            if isinstance(current, list)
            else []
        )
        status["business_health"] = health or [
            {"role": "ingress", "status": "unknown", "error": "no_fresh_datapath_evidence"}
        ]
        now = time.time()
        ack = state.acknowledgements()
        alerts = []
        for source in sources:
            if source["state"] not in {"observed", "disabled"}:
                ident = hashlib.sha256(
                    canonical(
                        [source["host"], source["source"], source.get("last_at"), source["state"]]
                    ).encode()
                ).hexdigest()[:32]
                alerts.append(
                    {
                        "id": ident,
                        "host": source["host"],
                        "source": source["source"],
                        "reason": source["state"],
                        "acknowledged_at": ack.get(ident),
                        "next_step": (
                            "检查采集服务、保留期限和传输状态；确认业务健康需查看独立链路证据。"
                        ),
                    }
                )
        try:
            daily = store.rollups(now - 7 * 86400, now)
            recent = store.query(
                {"since": now - 900, "until": now, "category": "system"}, limit=100
            )
            for row in recent["items"]:
                if row["kind"] in {
                    "service_failed",
                    "recovery_failed",
                    "protection_failed",
                    "health_degraded",
                }:
                    ident = row["record_id"][:32]
                    alerts.append(
                        {
                            "id": ident,
                            "host": row["host_id"],
                            "source": row.get("unit", row["source"]),
                            "reason": row["kind"],
                            "acknowledged_at": ack.get(ident),
                            "next_step": (
                                "查看同时间段系统事件和当前链路证据；历史异常不代表仍未恢复。"
                            ),
                        }
                    )
        except LogError:
            daily = {"items": [], "scope": "unavailable"}
        return {
            "request_summary": daily,
            "as_of": now,
            "control": status,
            "sources": sources,
            "policy": config,
            "alerts": alerts,
            "visibility": "仅连接元数据；不包含 HTTPS 正文、完整网页路径或消息内容。",
        }

    @app.get("/admin/api/devices")
    def devices(auth=Depends(authenticated)):
        return control.read("devices") | control.read("subscriptions")

    @app.get("/admin/api/devices/{device_id}")
    def device(device_id: ID, auth=Depends(authenticated)):
        records = control.read("devices")["devices"]
        selected = next((d for d in records if d["id"] == device_id), None)
        if selected is None:
            raise LogError("ADMIN_NOT_FOUND", status=404)
        return {
            "device": selected,
            "subscriptions": [
                s
                for s in control.read("subscriptions")["subscriptions"]
                if s["device_id"] == device_id
            ],
        }

    @app.get("/admin/api/devices/{device_id}/history", dependencies=[Depends(bounded)])
    def history(
        device_id: ID,
        since: float,
        until: float,
        grain: Literal["minute", "hour"] = "minute",
        auth=Depends(authenticated),
    ):
        return control.read("history", device=device_id, since=since, until=until, grain=grain)

    def logs(data, auth, kind=None, device=None):
        filters = dict(data.filters)
        if kind:
            filters["kind"] = kind
        if device:
            filters["device_id"] = device
        result = store.query(filters, cursor=data.cursor, limit=data.limit)
        state.note(auth["id"], "logs.query", device)
        return result

    @app.post("/admin/api/devices/{device_id}/connections", dependencies=[Depends(bounded)])
    def connections(device_id: ID, data: Query, auth=Depends(authenticated)):
        return logs(data, auth, "connection", device_id)

    @app.post("/admin/api/subscription-requests", dependencies=[Depends(bounded)])
    def requests(data: Query, auth=Depends(authenticated)):
        return logs(data, auth, "request")

    @app.post("/admin/api/events", dependencies=[Depends(bounded)])
    def events(data: Query, auth=Depends(authenticated)):
        return logs(data, auth)

    @app.get("/admin/api/sources")
    def sources(auth=Depends(authenticated)):
        return {"sources": store.sources(), "policy": store.policy()}

    @app.post("/admin/api/audit", dependencies=[Depends(bounded)])
    def audit(data: Query, auth=Depends(authenticated)):
        since, until = (
            data.filters.get("since", time.time() - 3600),
            data.filters.get("until", time.time()),
        )
        return {
            "admin": state.audit(since, until, limit=data.limit, cursor=data.cursor),
            "control": store.query(
                {"since": since, "until": until, "kind": "management"}, limit=data.limit
            ),
        }

    @app.post("/admin/api/actions")
    def action(data: Action, auth=Depends(authenticated)):
        if not data.confirmed:
            raise LogError("ADMIN_ACTION_CONFIRM")
        from wayvia.api import DeviceInput, SubscriptionInput

        try:
            if data.action == "device.add":
                DeviceInput.model_validate(data.payload)
                if set(data.payload) - {"name", "platform"}:
                    raise ValueError()
            elif data.action == "subscription.issue":
                SubscriptionInput.model_validate(data.payload)
                if set(data.payload) - {"device_id", "format", "lifetime"}:
                    raise ValueError()
            elif data.payload or data.object_id is None:
                raise ValueError()
        except ValueError:
            raise LogError("ADMIN_INPUT_INVALID") from None
        state.intent(auth["id"], data.key, data.action, data.object_id, data.payload)
        try:
            result = control.action(data.action, data.object_id, data.payload, data.key)
        except LogError:
            state.finish(auth["id"], data.key, data.action, data.object_id, "unknown")
            raise
        observed_id = result.get("id") if isinstance(result, dict) else None
        object_id = data.object_id
        if isinstance(observed_id, str) and re.fullmatch(r"[a-f0-9]{32}", observed_id):
            object_id = object_id or observed_id
        outcome = result.get("status", "submitted") if isinstance(result, dict) else "submitted"
        if data.action == "subscription.issue" and object_id:
            outcome = "issued"
        if outcome not in {
            "active",
            "pending",
            "revoked",
            "issued",
            "published",
            "applied",
            "failed",
            "degraded",
        }:
            outcome = "submitted"
        state.finish(auth["id"], data.key, data.action, object_id, outcome)
        return {
            "result": result,
            "audit_result": outcome,
            "note": "提交结果不等于数据面已生效；以设备状态和证据为准。",
        }

    @app.post("/admin/api/policy")
    def update(data: PolicyChange, auth=Depends(authenticated)):
        if not data.confirmed:
            raise LogError("ADMIN_ACTION_CONFIRM")
        old = state.intent(auth["id"], data.key, "policy.update", None, data.model_dump())
        if old and old["result"] == "applied":
            return store.policy()
        result = store.update_policy(data.changes, data.expected_revision)
        state.set_retention(result["audit_days"])
        state.finish(auth["id"], data.key, "policy.update", None, "applied")
        return result

    @app.post("/admin/api/alerts/{ident}/ack")
    def acknowledge(ident: ID, auth=Depends(authenticated)):
        return state.acknowledge(auth["id"], ident)

    @app.post("/admin/api/exports", dependencies=[Depends(bounded)])
    def export(data: Export, auth=Depends(authenticated)):
        return exports.create(state, store, auth["id"], data.filters, data.mode, data.confirmed)

    @app.get("/admin/api/exports/{ident}")
    def export_status(ident: ID, auth=Depends(authenticated)):
        return exports.lookup(state, auth["id"], ident)

    @app.get("/admin/api/exports/{ident}/download")
    def download(ident: ID, auth=Depends(authenticated)):
        exports.lookup(state, auth["id"], ident)
        state.note(auth["id"], "export.download", ident)
        return FileResponse(
            state.root / "exports" / (ident + ".json"),
            filename="wayvia-logs.json",
            media_type="application/json",
        )

    @app.delete("/admin/api/exports/{ident}")
    def remove_export(ident: ID, auth=Depends(authenticated)):
        return exports.delete(state, auth["id"], ident)

    return app
