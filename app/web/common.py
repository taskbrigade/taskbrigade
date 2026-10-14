from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup
from sqlalchemy.orm import Session

from app.auth import UiUser, csrf_token, current_user
from app.config import get_settings
from app.db import db
from app.errors import Forbidden
from app.i18n import LANGUAGES, N_, date_format, decimal, get_language, translate, translate_plural
from app.i18n import gettext as _
from app.models import Actor

templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _static_version() -> str:
    """Short hash of static assets that changes on updates to prevent stale browser caches."""
    import hashlib

    h = hashlib.sha256()
    for name in ("app.css", "i18n.js", "kanban.js", "form_guard.js", "sprint_editor.js", "task_form.js",
                 "model_fields.js", "brand/favicon.svg", "brand/app-icon.svg",
                 "brand/logo.png", "brand/mark.svg"):
        f = STATIC_DIR / name
        if f.exists():
            h.update(f.read_bytes())
    return h.hexdigest()[:10]


templates.env.globals["static_v"] = _static_version()

# Translations: {{ _("…") }} / {{ ngettext("…", "…", n) }} (newstyle: Jinja formats and escapes).
templates.env.add_extension("jinja2.ext.i18n")
templates.env.install_gettext_callables(translate, translate_plural, newstyle=True)
templates.env.globals.update(LANGUAGES=LANGUAGES, current_language=get_language)


def _local_time(value: datetime | None, fmt: str = "datetime") -> str:
    """Display DB UTC in TZ_DISPLAY. `fmt` is a named locale format (datetime, short, date,
    daytime; see app/i18n.py) or a strftime pattern."""
    if value is None:
        return "–"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    pattern = fmt if "%" in fmt else date_format(fmt)
    return value.astimezone(ZoneInfo(get_settings().tz_display)).strftime(pattern)


templates.env.filters["localtime"] = _local_time

# Disable raw HTML to prevent scripts in agent text; markdown-it rejects javascript: links.
_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable(["table", "strikethrough"])


def render_markdown(text: str | None) -> Markup:
    return Markup(_md.render(text or ""))


templates.env.filters["markdown"] = render_markdown


def _filesize(n: int | None) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{decimal(n)} {unit}"
        n /= 1024
    return ""


templates.env.filters["filesize"] = _filesize

STATUS_LABELS = {"draft": N_("Draft"), "defined": N_("Defined"), "in_progress": N_("In progress"),
                 "questions": N_("Question"), "aborted": N_("Aborted (agent)"), "for_review": N_("Review"),
                 "changes_requested": N_("Rework"), "done": N_("Done"), "cancelled": N_("Cancelled")}
PROJECT_STATUS_LABELS = {"idea": N_("Idea"), "active": N_("Active"), "paused": N_("Paused"),
                         "archived": N_("Archived")}

SPRINT_STATUS_LABELS = {"planned": N_("Planned"), "released": N_("Released"), "closed": N_("Closed")}
FIELD_LABELS = {"description": N_("Description"), "acceptance_criteria": N_("Acceptance criteria")}
PRIO_LABELS = {1: N_("1 – highest"), 2: N_("2 – high"), 3: N_("3 – normal"), 4: N_("4 – low"), 5: N_("5 – lowest")}


def status_label(status: str) -> str:
    """Return the task status label in the current language; preserve unknown values."""
    return _(STATUS_LABELS[status]) if status in STATUS_LABELS else status


def project_status_label(status: str) -> str:
    return _(PROJECT_STATUS_LABELS[status]) if status in PROJECT_STATUS_LABELS else status


def sprint_status_label(status: str) -> str:
    return _(SPRINT_STATUS_LABELS[status]) if status in SPRINT_STATUS_LABELS else status


def prio_label(prio: int) -> str:
    return _(PRIO_LABELS[prio]) if prio in PRIO_LABELS else str(prio)


def field_labels(fields: list[str]) -> str:
    """Task field names (e.g. `missing_definition`) as a readable list in the current language."""
    return ", ".join(_(FIELD_LABELS[f]) if f in FIELD_LABELS else f for f in fields)


# Dialog texts for app/static/*.js: JSON in base.html, read by app/static/i18n.js.
JS_MESSAGES = (
    N_("Error %(status)s"), N_("Enter text:"), N_("Set the status manually anyway (admin override)?"),
    N_("Saving failed – please check the message on the page. The action is cancelled."),
    N_("The new task has not been created yet."), N_("The changes to this task are not saved yet."),
    N_("Save now?\n(OK = save and continue, Cancel = do not save)"),
    N_("Discard the input and continue?\n(Cancel = stay on this page)"), N_("Save and then release?"),
    N_("Discard the unsaved changes?"),
    # Sprint editor (app/static/sprint_editor.js).
    N_("The changes to this sprint are not saved yet."), N_("ready"), N_("missing: %(fields)s"),
    N_("Description"), N_("Acceptance criteria"), N_("New title:"), N_("Remove"), N_("Drag to reorder"),
    N_("%(task_nr)s is cancelled when you save the sprint. Remove it?"),
    # Kanban "Move" menu (app/static/kanban.js).
    N_("Loading…"),
)


def js_messages() -> dict[str, str]:
    return {m: _(m) for m in JS_MESSAGES}


def _tier_label(tier: str | None) -> str:
    from app.services.model_choice import tier_label

    return tier_label(tier)


templates.env.globals.update(status_label=status_label, project_status_label=project_status_label,
                             sprint_status_label=sprint_status_label, field_labels=field_labels,
                             prio_label=prio_label,
                             js_messages=js_messages, tier_label=_tier_label)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def ui_actor(session: Session, user: UiUser) -> Actor:
    """Resolve the actor for the UI session (permissions, events, comments)."""
    actor = session.get(Actor, user.actor_id) if user.actor_id else None
    if actor is None or not actor.is_active:
        raise Forbidden(_("This session is not linked to an active actor – sign in as an actor"))
    return actor


def ui_actor_or_none(session: Session, user: UiUser) -> Actor | None:
    """For read-only pages, sessions without an actor (bootstrap admin after setup) can see all."""
    actor = session.get(Actor, user.actor_id) if user.actor_id else None
    return actor if actor is not None and actor.is_active else None


def quick_capture_projects(actor_id: int | None = None) -> list[tuple[str, str]]:
    """Projects for the quick capture "+"; only own ones while the human's "Show all projects" switch is off."""
    if not db.ready:
        return []
    from app.models import Project
    from app.services import access
    from sqlalchemy import select

    with db.session() as s:
        stmt = select(Project.project_key, Project.name).where(Project.status.in_(("idea", "active", "paused")))
        me = s.get(Actor, actor_id) if actor_id else None
        if access.own_projects_only(me):
            stmt = stmt.where(Project.owner_id == me.id)
        return list(s.execute(stmt.order_by(Project.project_key)).all())


@contextmanager
def web_session() -> Iterator[Session]:
    """DB session for UI handlers: commit on success, otherwise roll back."""
    session = db.session()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx.setdefault("user", current_user(request))
    ctx.setdefault("db_ready", db.ready)
    if ctx["db_ready"] and ctx["user"] is not None and not is_htmx(request):
        ctx.setdefault("qc_projects", quick_capture_projects(getattr(ctx["user"], "actor_id", None)))
    ctx["csrf"] = csrf_token(request)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
