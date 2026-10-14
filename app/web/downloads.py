"""Agent onboarding without Gitea access: `GET /install.sh` and the tbr wheel under `/download/`.

No login: the script runs before the agent has an identity. The network policy (`app.main.network_policy`) only
lets peers through that may also use the API.
"""
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, PlainTextResponse

from app.db import db
from app.i18n import gettext as _
from app.services import distribution

router = APIRouter()


def server_url(request: Request) -> str:
    """Configured TaskBrigade URL (Admin → Settings), otherwise the address the request came in on."""
    if db.ready:
        from app.services import settings_store
        from app.web.common import web_session

        try:
            with web_session() as s:
                configured = settings_store.get(s, "app_url")
            if configured:
                return configured.rstrip("/")
        except Exception:  # noqa: BLE001 – the script must work with a broken settings table, too.
            pass
    return str(request.base_url).rstrip("/")


def _unavailable() -> PlainTextResponse:
    return PlainTextResponse(_("The tbr package of this server is not available – run deploy/update.sh or rebuild "
                               "the image (details in the server log)") + "\n", status_code=503)


@router.get("/install.sh")
def install_sh(request: Request):
    version = distribution.app_version()
    if not version or distribution.ensure_wheel() is None:
        return _unavailable()
    return PlainTextResponse(distribution.install_script(server_url(request), version, distribution.build_id()),
                             media_type="text/x-shellscript", headers={"Cache-Control": "no-store"})


@router.get("/download/{filename}")
def download(filename: str):
    version = distribution.app_version()
    if version and filename == distribution.legacy_wheel_name(version):  # `tm self-update` from before the rename
        wheel = distribution.ensure_legacy_wheel()
    elif version and filename == distribution.wheel_name(version):
        wheel = distribution.ensure_wheel()
    else:
        return PlainTextResponse(_("Only %(name)s is available here", name=distribution.wheel_name(version or "?"))
                                 + "\n", status_code=404)
    if wheel is None:
        return _unavailable()
    # Same name for every build of a version: caches must revalidate (tbr self-update also passes --reinstall-package).
    return FileResponse(wheel, media_type="application/octet-stream", filename=filename,
                        headers={"Cache-Control": "no-cache"})
