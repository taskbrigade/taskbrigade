"""FastAPI app: routers, setup mode, and scheduler startup."""
import logging
import secrets
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api.router import api_router
from app.auth import LoginRequired
from app.config import get_settings
from app.db import db
from app.errors import AppError
from app.i18n import _, LanguageMiddleware
from app.logging_setup import setup_logging
from app.scheduler import create_scheduler
from app.services import distribution, telegram_bot
from app.web import (admin_agents, admin_db, admin_settings, admin_users, auth_routes, downloads, profile,
                     projects_ui, sprint_editor, sprints_ui, tasks_ui)

log = logging.getLogger(__name__)


def app_version() -> str:
    return distribution.app_version() or _("unknown")

# Paths accessible in setup mode (prefixes).
SETUP_ALLOWED = ("/healthz", "/login", "/logout", "/language", "/static/", "/admin/database", "/install.sh",
                 "/download/")
# tbr package delivery (app/web/downloads.py): only for peers that may also use the API.
AGENT_DOWNLOADS = ("/install.sh", "/download/")
# Response header with the server version; `tbr` warns once per call when it is older (cli/client.py).
VERSION_HEADER = "X-TaskBrigade-Version"
BUILD_HEADER = "X-TaskBrigade-Build"  # Build of the delivered wheel; differs after a change without a version bump.
# Names before TaskBrigade 0.8, sent as well: installed tbr/tm before 0.8 read only these for their update warning.
LEGACY_VERSION_HEADER = "X-Taskmeister-Version"
LEGACY_BUILD_HEADER = "X-Taskmeister-Build"


def network_policy(request: Request):
    """Decide based on the direct peer (uvicorn runs without --proxy-headers):
    localhost (tailscale serve or local agents), TRUSTED_PEER_NETWORKS or API_NETWORKS → all; LAN_UI_NETWORKS → UI only
    (no API, no tbr download/install.sh); otherwise 403. Only localhost and TRUSTED_PEER_NETWORKS may send Tailscale-User-Login (app/auth.py)."""
    settings = get_settings()
    if not settings.enforce_client_networks:
        return None
    host = request.client.host if request.client else ""
    if settings.is_trusted_peer(host) or settings.is_api_peer(host):
        return None
    try:
        addr = ip_address(host)
    except ValueError:
        addr = None
    if addr is not None and any(addr in net for net in settings.lan_networks):
        if request.url.path.startswith("/api/"):
            return error_response(403, "lan_api_forbidden",
                                  _( 'The API is not available over HTTP on the LAN – use Tailscale (HTTPS)' ))
        if request.url.path.startswith(AGENT_DOWNLOADS):
            return PlainTextResponse(_("tbr can only be downloaded from networks that may use the API – use "
                                       "Tailscale (HTTPS)") + "\n", status_code=403)
        return None
    return PlainTextResponse(_( 'Access is only allowed through Tailscale or the permitted LAN' ), status_code=403)


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.api_networks:
        log.warning("api_networks_cleartext", extra={"api_networks": settings.api_networks})
    result = db.init_from_settings(settings)
    log.info("startup", extra={"db_ok": result.ok, "db_ready": db.ready, "db_error": result.error})
    scheduler = create_scheduler() if settings.scheduler_enabled else None
    if scheduler:
        scheduler.start()
        telegram_bot.start()  # Long polling in its own thread; Telegram problems never reach the server.
    yield
    if scheduler:
        telegram_bot.shutdown()
        scheduler.shutdown(wait=False)
    db.dispose()


def create_app() -> FastAPI:
    setup_logging()
    settings = get_settings()
    secret = settings.secret_key
    if not secret:
        log.warning(_("SECRET_KEY missing – using a temporary key; sessions will not survive a restart"))
        secret = secrets.token_urlsafe(48)

    app = FastAPI(title="TaskBrigade", version=app_version(), lifespan=lifespan)

    @app.middleware("http")
    async def setup_mode(request: Request, call_next):
        blocked = network_policy(request)
        if blocked is not None:
            return blocked
        response = await _setup_mode(request, call_next)
        if distribution.app_version():
            for name in (VERSION_HEADER, LEGACY_VERSION_HEADER):
                response.headers[name] = distribution.app_version()
            build = distribution.build_id()
            if build:
                for name in (BUILD_HEADER, LEGACY_BUILD_HEADER):
                    response.headers[name] = build
        return response

    async def _setup_mode(request: Request, call_next):
        path = request.url.path
        if not db.ready:
            if path.startswith("/api/"):
                return error_response(503, "db_unavailable", _( 'Database unavailable (setup mode)' ))
            if not path.startswith(SETUP_ALLOWED) and path not in ("/docs", "/openapi.json"):
                return RedirectResponse("/admin/database", 303)
        return await call_next(request)

    # add_middleware wraps existing layers, so register the session after setup_mode.
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="lax",
                       max_age=12 * 3600, session_cookie="tm_session")
    # Keep language outermost so it also applies to error pages and setup mode.
    app.add_middleware(LanguageMiddleware)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", 303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            code = exc.headers.get("X-Error-Code", "error") if exc.headers else "error"
            return error_response(exc.status_code, code, str(exc.detail))
        return _ui_error(request, exc.status_code, str(exc.detail))

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        if request.url.path.startswith("/api/"):
            return error_response(exc.status, exc.code, exc.message)
        return _ui_error(request, exc.status, exc.message)

    def _ui_error(request: Request, status: int, message: str):
        from app.web.common import is_htmx, render

        if is_htmx(request):  # htmx-config swaps 422 responses so the message appears in the target.
            return render(request, "_flash.html", 422, message=message, kind="error")
        return render(request, "error.html", status, message=message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        return error_response(422, "validation_error", str(exc.errors()))

    @app.get("/healthz")
    def healthz():
        from app.scheduler import gitea_status

        gitea = {True: "ok", False: "unavailable", None: "unknown"}[gitea_status["ok"]]
        body = {"version": app_version(), "build": distribution.build_id() or None,
                "db": "ok" if db.ready else "unavailable", "gitea": gitea}
        return JSONResponse(body, status_code=200 if db.ready else 503)

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(api_router)
    app.include_router(auth_routes.router)
    app.include_router(downloads.router)
    app.include_router(admin_db.router)
    app.include_router(admin_agents.router)
    app.include_router(admin_users.router)
    app.include_router(admin_settings.router)
    app.include_router(tasks_ui.router)
    app.include_router(projects_ui.router)
    app.include_router(sprints_ui.router)
    app.include_router(sprint_editor.router)
    app.include_router(profile.router)
    return app


app = create_app()
