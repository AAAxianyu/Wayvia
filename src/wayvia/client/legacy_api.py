"""API-only client contract overlay for the reviewed schema-3 production baseline.

Run with the unchanged schema-3 package plus only client helpers and ProfileRenderer.
This entry point deliberately refuses a newer storage runtime. It never starts an
agent, collector or migration, and retains the baseline's local management routes.
"""

import argparse
import hashlib
import ipaddress
import sqlite3
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response

from wayvia.client import contract, headers, https_url, title
from wayvia.client.names import display_name
from wayvia.errors import ExitCode, WayviaError


def _account(service, token, format=None):
    with service.db.connect() as connection:
        row = connection.execute(
            "SELECT * FROM subscriptions WHERE token_hash=?",
            (hashlib.sha256(token.encode()).hexdigest(),),
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
    device = service.device(row["device_id"])
    if device["status"] != "active" or device["desired_action"]:
        raise WayviaError(
            "subscription_denied", "Subscription is unavailable", exit_code=ExitCode.AUTH
        )
    service._published_config()
    return row, device


def create_legacy_app(service, admin_token, *, subscriptions_only=False, public_origin=None):
    from wayvia.api import create_app

    if public_origin is not None:
        origin = https_url(public_origin)
        if origin.path not in {"", "/"} or origin.query:
            raise WayviaError("CLIENT_BASE_URL_INVALID", "Expected an HTTPS origin")
        public_origin = public_origin.rstrip("/")
    app = create_app(service, admin_token, subscriptions_only=subscriptions_only)
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if getattr(route, "path", None)
        not in {"/subscriptions/{token}/{format}", "/subscriptions/{token}/info"}
    ]

    @app.middleware("http")
    async def private_metadata(request: Request, call_next):
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/subscriptions/{token}/info")
    def info(token: str):
        row, device = _account(service, token)
        return {
            "schema_version": 1,
            "title": title(device["name"], display_name(service.db, row["id"])),
            "format": row["format"],
            "download_link_expires_at": row["expires_at"],
            "device_status": device["status"],
            "usage": device["usage"],
            "client": contract(),
        }

    @app.get("/subscriptions/{token}/{format}")
    def fetch(request: Request, token: str, format: str):
        row, device = _account(service, token, format)
        text = service.fetch(token, format)
        response_headers = headers(device["name"], format, display_name(service.db, row["id"]))
        if format == "mihomo":
            path = request.url.path.rsplit("/", 1)[0] + "/info"
            if public_origin is not None:
                response_headers["profile-web-page-url"] = public_origin + path
            elif request.url.scheme == "https":
                response_headers["profile-web-page-url"] = str(
                    request.url.replace(path=path, query="", fragment="")
                )
        return Response(
            text,
            media_type="application/json" if format == "sing-box" else "application/yaml",
            headers=response_headers,
        )

    return app


def main():
    import uvicorn

    from wayvia.config import load_config
    from wayvia.secrets import SecretStore
    from wayvia.services.control import ControlService
    from wayvia.storage import MIGRATIONS

    if set(MIGRATIONS) != {1, 2, 3}:
        raise WayviaError("legacy_baseline_required", "Use the reviewed schema-3 API release")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/var/lib/wayvia/control"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--public-origin", help="HTTPS origin including the published port")
    args = parser.parse_args()
    if not ipaddress.ip_address(args.host).is_loopback:
        raise WayviaError("local_bind_required", "API must bind a loopback address")
    with sqlite3.connect(f"file:{args.root / 'metadata.db'}?mode=ro", uri=True) as connection:
        versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    if versions != {1, 2, 3}:
        raise WayviaError("legacy_database_required", "Expected an existing schema-3 database")
    service = ControlService(args.root, load_config(args.root / "config.yaml"), mode="production")
    admin_token = SecretStore(args.root / "secrets").read("admin-token")
    uvicorn.run(
        create_legacy_app(service, admin_token, public_origin=args.public_origin),
        host=args.host,
        port=args.port,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
