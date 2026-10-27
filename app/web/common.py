import re
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
from app.i18n import (
    LANGUAGES,
    N_,
    date_format,
    decimal,
    default_language,
    get_language,
    ngettext,
    normalize,
    relative_time,
    translate,
    translate_plural,
)
from app.i18n import gettext as _
from app.models import Actor
from app.services.zones import MODEL_LOCATION_LABELS

templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _static_version() -> str:
    """Short hash of static assets that changes on updates to prevent stale browser caches."""
    import hashlib

    h = hashlib.sha256()
    for name in ("app.css", "ui.css", "header.css", "backlog.css", "backlog.js", "profile.css", "shell.js", "i18n.js", "kanban.js", "planning.js", "form_guard.js",
                 "sprint_editor.js", "task_form.js", "settings.js", "workflow_editor.js", "workflow_actions.js", "workflows.css", "admin_users.css", "model_fields.js",
                 "brand/favicon.svg", "brand/app-icon.svg", "brand/logo.png", "brand/mark.svg"):
        f = STATIC_DIR / name
        if f.exists():
            h.update(f.read_bytes())
    return h.hexdigest()[:10]


templates.env.globals["static_v"] = _static_version()

# Translations: {{ _("…") }} / {{ ngettext("…", "…", n) }} (newstyle: Jinja formats and escapes).
templates.env.add_extension("jinja2.ext.i18n")
templates.env.install_gettext_callables(translate, translate_plural, newstyle=True)
templates.env.globals.update(LANGUAGES=LANGUAGES, current_language=get_language,
                             default_language=default_language, normalize_language=normalize)
templates.env.globals["model_locations"] = MODEL_LOCATION_LABELS  # Agent forms, users list, settings findings


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


def _rel_time(value: datetime | None) -> str:
    """Short relative age ("12 min ago") for inbox cards and notices; absolute time when older than a week."""
    if value is None:
        return "–"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - value
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return _("just now")
    if seconds < 3600:
        return ngettext("%(num)d minute ago", "%(num)d minutes ago", seconds // 60)
    if seconds < 86400:
        return ngettext("%(num)d hour ago", "%(num)d hours ago", seconds // 3600)
    if seconds < 7 * 86400:
        return ngettext("%(num)d day ago", "%(num)d days ago", seconds // 86400)
    return _local_time(value, "short")


templates.env.filters["reltime"] = _rel_time


def _relative_time(value: datetime | None) -> str:
    if value is None:
        return "–"
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    seconds = max(0, int((datetime.now(UTC) - value).total_seconds()))
    if seconds < 60:
        return _("just now")
    if seconds < 3600:
        return _("%(count)s min ago", count=seconds // 60)
    if seconds < 86400:
        return _("%(count)s h ago", count=seconds // 3600)
    return _("%(count)s d ago", count=seconds // 86400)


templates.env.filters["relative_time"] = _relative_time

# Disable raw HTML to prevent scripts in agent text; markdown-it rejects javascript: links.
_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable(["table", "strikethrough"])


def render_markdown(text: str | None) -> Markup:
    return Markup(_md.render(text or ""))


def render_inline(text: str | None) -> Markup:
    """One line of Markdown (checkbox labels). HTML stays escaped; a single paragraph is unwrapped."""
    html = _md.render(text or "").strip()
    if html.startswith("<p>") and html.endswith("</p>") and html.count("<p>") == 1:
        html = html[3:-4]
    return Markup(html)


_CHECKBOX_LINE = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+(.*?)\s*$")
_BULLET_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*?)\s*$")


def criteria_view(text: str | None) -> dict:
    """Split acceptance criteria into checkbox items (with a met-count) or leave them as Markdown.

    Checkbox lines (`- [ ]` / `- [x]`) become a checklist. Plain bullets and other lines stay with them when a
    checkbox is present; text with no checkbox is rendered as Markdown by the template instead.
    """
    items: list[dict] = []
    checks = met = 0
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        box = _CHECKBOX_LINE.match(line)
        if box:
            done = box.group(1).lower() == "x"
            items.append({"kind": "check", "done": done, "text": box.group(2)})
            checks += 1
            met += int(done)
            continue
        bullet = _BULLET_LINE.match(line)
        items.append({"kind": "bullet" if bullet else "text", "done": False,
                      "text": bullet.group(1) if bullet else line.strip()})
    return {"items": items, "checks": checks, "met": met, "empty": not (text or "").strip()}


templates.env.filters["markdown"] = render_markdown
templates.env.filters["inline_md"] = render_inline
templates.env.filters["criteria_view"] = criteria_view


templates.env.filters["reltime"] = relative_time


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
    N_("Could not mark notices as read – please reload."),
    N_("Could not undo marking notices as read – please reload."),
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
    # Planning accordion (app/static/planning.js).
    N_("%(done)d / %(total)d done"),
    # Kanban "Move" menu (app/static/kanban.js).
    N_("Loading…"), N_("Saved"),
    # Relative times of the projects overview (`i18n.relative_time`; translated at run time, not by a literal _()).
    N_("today"), N_("yesterday"), N_("%(num)d days ago"),
    # Workflow editor (app/static/workflow_editor.js, form_guard.js).
    N_("The changes to this workflow are not saved yet."), N_("Remove this step?"), N_("Insert %(placeholder)s"),
    N_("No placeholders available yet."),
    # Admin → Settings (app/static/settings.js, form_guard.js).
    N_("The changes to the settings are not saved yet."), N_("No unsaved changes"), N_("1 unsaved change"),
    N_("%(n)s unsaved changes"), N_("Cancel"),
    # Project page (app/static/project_detail.js, form_guard.js).
    N_("The changes to this project are not saved yet."), N_("Copied"), N_("Press Ctrl+C to copy"),
)


def js_messages() -> dict[str, str]:
    return {m: _(m) for m in JS_MESSAGES}


def _tier_label(tier: str | None) -> str:
    from app.services.model_choice import tier_label

    return tier_label(tier)


def _duration(seconds: int | None) -> str:
    """Short duration for workflow steps and runs: 41s, 12m, 3h 5m, 2d 4h."""
    if seconds is None:
        return "–"
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes, hours, days = seconds // 60, seconds // 3600, seconds // 86400
    if hours < 1:
        return f"{minutes}m"
    if days < 1:
        return f"{hours}h {minutes % 60}m"
    return f"{days}d {hours % 24}h"


templates.env.filters["wf_duration"] = _duration


def _wf_label(kind: str):
    def label(value: str | None) -> str:
        from app.services.workflows import ui

        table = {"run": ui.RUN_STATUS_LABELS, "step": ui.STEP_STATUS_LABELS, "type": ui.STEP_TYPE_LABELS,
                 "trigger": ui.TRIGGER_LABELS, "review": ui.REVIEW_LABELS}[kind]
        return _(table[value]) if value in table else (value or "–")
    return label


# Workflow labels (menu "Workflows", run chain on the task page).
templates.env.globals.update(run_status_label=_wf_label("run"), step_status_label=_wf_label("step"),
                             wf_type_label=_wf_label("type"), wf_trigger_label=_wf_label("trigger"),
                             wf_review_label=_wf_label("review"))

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
            stmt = stmt.where(Project.owned_by(me.id))
        return list(s.execute(stmt.order_by(Project.project_key)).all())


def data_menu(actor_id: int | None) -> bool:
    """Menu item "Data": data store on and the human has the data right, owns a project or is an admin."""
    if not db.ready or not actor_id:
        return False
    from app.services import datasets

    try:
        with db.session() as s:
            return datasets.menu_visible(s, s.get(Actor, actor_id))
    except Exception:  # noqa: BLE001 – the menu must never break a page (e.g. schema not migrated yet)
        return False


def planning_menu(actor_id: int | None) -> bool:
    """Menu item "Planning": the actor may plan sprints in at least one project."""
    if not db.ready or not actor_id:
        return False
    from app.web.planning_ui import may_plan_somewhere

    try:
        with db.session() as s:
            return may_plan_somewhere(s, s.get(Actor, actor_id))
    except Exception:  # noqa: BLE001 – the menu must never break a page
        return False


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
        with db.session() as menu_session:
            from app.services import settings_store
            ctx.setdefault("show_workflows", settings_store.get_bool(menu_session, "workflows_enabled"))
        ctx.setdefault("show_data", data_menu(getattr(ctx["user"], "actor_id", None)))
        ctx.setdefault("show_planning", planning_menu(getattr(ctx["user"], "actor_id", None)))
        ctx.setdefault("inbox_badge", inbox_badge(request, ctx))
    ctx["csrf"] = csrf_token(request)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def inbox_badge(request: Request, ctx: dict) -> int:
    """Match the Inbox's Needs your action count, including its owner/sprint scope."""
    from app.services import output_gate, result_mail, tasks
    from app.web.tasks_ui import _owner_ctx, _sprint_param, workflow_group

    if "inbox" in ctx:
        data = ctx["inbox"]
        mails, releases = ctx.get("mail_approvals", []), ctx.get("data_releases", [])
    else:
        with db.session() as session:
            me = ui_actor_or_none(session, ctx["user"])
            owner_id = _owner_ctx(session, request, me)["owner_id"] if me else None
            data = workflow_group(tasks.inbox(session, owner_id, _sprint_param(request)))
            mails = result_mail.pending_for(session, me)
            releases = output_gate.pending_for(session, me)
    # Reviews by reviewer agents are not counted: they do not need the user (own Inbox section).
    keys = ("questions", "for_review", "aborted")
    return sum(len(data.get(key, [])) for key in keys) + len(mails) + len(releases)
