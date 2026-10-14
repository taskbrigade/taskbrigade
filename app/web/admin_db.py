from fastapi import APIRouter, Depends, Form, Request

from app import dbconfig
from app.auth import UiUser, csrf_protect, require_admin
from app.config import get_settings
from app.db import db, upgrade_schema
from app.i18n import gettext as _
from app.logging_setup import audit
from app.services import database_admin
from app.services.database_admin import DbForm
from app.web.common import render

router = APIRouter(prefix="/admin/database")


def _form(host: str = Form(...), port: int = Form(...), database: str = Form(...), user: str = Form(...),
          password: str = Form(""), change_in_db: bool = Form(False)) -> DbForm:
    return DbForm(host, port, database, user, password, change_in_db)


@router.get("")
def page(request: Request, _user: UiUser = Depends(require_admin)):
    db.recheck()
    return render(request, "admin/database.html", cfg=dbconfig.load(get_settings()).public(), probe=db.last_probe,
                  section="database")


@router.post("/test", dependencies=[Depends(csrf_protect)])
def test(request: Request, form: DbForm = Depends(_form), user: UiUser = Depends(require_admin)):
    result = database_admin.test(get_settings(), form)
    audit(user.name, "db_test", host=form.host, database=form.database, db_user=form.user, ok=result.ok)
    return render(request, "admin/_db_test_result.html", result=result)


@router.post("/save", dependencies=[Depends(csrf_protect)])
def save(request: Request, form: DbForm = Depends(_form), user: UiUser = Depends(require_admin)):
    outcome = database_admin.save(get_settings(), form)
    audit(user.name, "db_save", host=form.host, database=form.database, db_user=form.user,
          changed_in_db=form.change_in_db, ok=outcome.ok)
    return render(request, "admin/_db_status.html", probe=db.last_probe, message=outcome.message,
                  message_kind="ok" if outcome.ok else "error")


@router.post("/upgrade", dependencies=[Depends(csrf_protect)])
def upgrade(request: Request, user: UiUser = Depends(require_admin)):
    if db.engine is None or not db.last_probe.ok:
        return render(request, "admin/_db_status.html", probe=db.last_probe,
                      message=_("No connection – save the credentials first."), message_kind="error")
    try:
        head = upgrade_schema(db.engine)
        message, kind = _("Schema upgraded to %(head)s.", head=head), "ok"
    except Exception as e:
        message, kind = _("Schema upgrade failed: %(error)s", error=f"{type(e).__name__}: {e}"), "error"
    audit(user.name, "db_upgrade", ok=kind == "ok")
    db.recheck()
    return render(request, "admin/_db_status.html", probe=db.last_probe, message=message, message_kind=kind)
