"""Admin → UI kit: showcase of the shared redesign components (templates/_ui.html, static/ui.css)."""
from fastapi import APIRouter, Depends, Request

from app.auth import require_admin
from app.web.common import render

router = APIRouter(prefix="/admin/ui-kit", dependencies=[Depends(require_admin)])


@router.get("")
def page(request: Request):
    return render(request, "admin/ui_kit.html", section="ui_kit")
