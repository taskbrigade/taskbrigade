from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app.auth import (bootstrap_user, csrf_protect, current_user, login_limiter, login_session,
                      ui_user_from_actor, verify_password)
from app.config import get_settings
from app.db import db
from app.i18n import LANG_COOKIE, normalize
from app.i18n import gettext as _
from app.logging_setup import audit
from app.web.common import render

router = APIRouter()

BOOTSTRAP_USER = "admin"


@router.get("/login")
def login_form(request: Request):
    if current_user(request):
        return RedirectResponse("/", 303)
    return render(request, "login.html")


@router.post("/login", dependencies=[Depends(csrf_protect)])
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    key = request.client.host if request.client else "?"
    if login_limiter.blocked(key):
        return render(request, "login.html", 429, error=_("Too many failed attempts – please try again later."),
                      username=username)
    user = None
    if username == BOOTSTRAP_USER and verify_password(get_settings().bootstrap_admin_password_hash, password):
        user = bootstrap_user()
    elif db.ready:  # An actor named `admin` can also sign in with their own password.
        from app.services.actors import authenticate_password

        with db.session() as s:
            actor = authenticate_password(s, username.strip().lower(), password)
            if actor is not None:
                user = ui_user_from_actor(actor)
    if user is not None:
        login_limiter.reset(key)
        login_session(request, user)
        audit(user.name, "login", method="password", bootstrap=user.bootstrap)
        return RedirectResponse("/", 303)
    login_limiter.fail(key)
    return render(request, "login.html", 401, error=_("Wrong username or password."), username=username)


@router.get("/language")
def set_language(lang: str, next: str = "/"):
    """Set the browser's language cookie; allow only relative return destinations."""
    target = next if next.startswith("/") and not next.startswith(("//", "/\\")) else "/"
    response = RedirectResponse(target, 303)
    code = normalize(lang)
    if code:
        response.set_cookie(LANG_COOKIE, code, max_age=365 * 24 * 3600, samesite="lax", httponly=True)
    else:  # For example, lang=default restores the default language.
        response.delete_cookie(LANG_COOKIE)
    return response


@router.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)
