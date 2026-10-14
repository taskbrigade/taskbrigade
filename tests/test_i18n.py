"""Translation layer: complete catalog, templates render in both languages, language selection."""
import ast
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app import i18n
from app.config import get_settings
from app.i18n import catalog, gettext, ngettext, track_missing, use_language
from app.models import TASK_STATUSES
from app.errors import Unprocessable
from app.services.notifications import FOREVER
from app.services.state_machine import require_definition
from app.web.common import STATUS_LABELS, status_label, templates
from app.web.tasks_ui import DONE_RANGES, _kanban_columns

APP = Path(__file__).resolve().parent.parent / "app"
TEMPLATES = APP / "templates"
UI_TEMPLATES = sorted(p.relative_to(TEMPLATES).as_posix() for p in TEMPLATES.rglob("*.html"))
TRANSLATED = [lang for lang in i18n.LANGUAGES if lang != i18n.SOURCE_LANGUAGE]
PLACEHOLDER = re.compile(r"%\(\w+\)[sd]")


# --- Catalog -------------------------------------------------------------------------------------------

def _extract() -> dict[str, tuple[str, bool]]:
    """Map msgid → (source location, plural?) from templates (`_`, `ngettext`) and Python files
    (`_`, `N_`, `gettext`, `ngettext` with a literal)."""
    found: dict[str, tuple[str, bool]] = {}
    for name in UI_TEMPLATES:
        for lineno, func, msg in templates.env.extract_translations((TEMPLATES / name).read_text()):
            first = msg if isinstance(msg, str) else (msg[0] if msg else None)
            if first:  # None means dynamic text, e.g. _(s.label); the source marks it with N_().
                found.setdefault(first, (f"{name}:{lineno}", func == "ngettext"))
    for path in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("_", "N_", "gettext", "ngettext") and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                found.setdefault(node.args[0].value,
                                 (f"{path.relative_to(APP.parent)}:{node.lineno}", node.func.id == "ngettext"))
    return found


MSGIDS = _extract()


def test_extraction_finds_ui_texts():
    assert len(MSGIDS) > 300
    assert "Log in" in MSGIDS and STATUS_LABELS["in_progress"] in MSGIDS and MSGIDS["%(num)d task"][1]


@pytest.mark.parametrize("lang", TRANSLATED)
def test_catalog_complete(lang):
    cat = catalog(lang)
    missing = sorted(f"{where}: {msgid!r}" for msgid, (where, _plural) in MSGIDS.items() if msgid not in cat)
    assert not missing, f"{len(missing)} Übersetzung(en) fehlen in app/locales/{lang}.json:\n" + "\n".join(missing)
    unused = sorted(set(cat) - set(MSGIDS))
    assert not unused, f"Nicht mehr verwendete Einträge in app/locales/{lang}.json: {unused}"


@pytest.mark.parametrize("lang", TRANSLATED)
def test_catalog_entries_well_formed(lang):
    problems = []
    for msgid, entry in catalog(lang).items():
        where, plural = MSGIDS.get(msgid, ("?", False))
        forms = entry if isinstance(entry, list) else [entry]
        if plural != isinstance(entry, list) or (plural and len(entry) != 2):
            problems.append(f"{where}: {msgid!r} – {'Liste mit 2 Pluralformen' if plural else 'Text'} erwartet")
        for form in forms:
            if not isinstance(form, str) or not form.strip():
                problems.append(f"{where}: {msgid!r} – leere Übersetzung")
            elif set(PLACEHOLDER.findall(form)) - set(PLACEHOLDER.findall(msgid)):
                problems.append(f"{where}: {msgid!r} – unbekannter Platzhalter in {form!r}")
            elif not plural and set(PLACEHOLDER.findall(form)) != set(PLACEHOLDER.findall(msgid)):
                problems.append(f"{where}: {msgid!r} – Platzhalter fehlen in {form!r}")
            elif form.count("<") != msgid.count("<"):
                problems.append(f"{where}: {msgid!r} – HTML-Tags stimmen nicht überein")
    assert not problems, "\n".join(problems)


# --- API -----------------------------------------------------------------------------------------------

def test_gettext_and_plurals():
    with use_language("en"):
        assert gettext("Saved.") == "Saved."
        assert gettext("Log out %(name)s", name="alex") == "Log out alex"
        assert ngettext("%(num)d task", "%(num)d tasks", 1) == "1 task"
        assert ngettext("%(num)d task", "%(num)d tasks", 0) == "0 tasks"
        assert status_label("in_progress") == "In progress" and status_label("weird") == "weird"
    with use_language("de"):
        assert gettext("Saved.") == "Gespeichert."
        assert gettext("Log out %(name)s", name="alex") == "alex abmelden"
        assert ngettext("%(num)d task", "%(num)d tasks", 1) == "1 Task"
        assert ngettext("%(num)d attachment stored in the repo", "%(num)d attachments stored in the repo", 3) == \
            "3 Anhänge im Repo abgelegt"
        assert status_label("in_progress") == "In Arbeit"


def test_service_error_code_is_stable_across_languages():
    task = SimpleNamespace(task_nr="DEMO-0001", description="", acceptance_criteria="")
    messages = {}
    for language in ("en", "de"):
        with use_language(language), pytest.raises(Unprocessable) as caught:
            require_definition(task)
        assert caught.value.code == "definition_incomplete"
        messages[language] = caught.value.message
    assert "Description and Acceptance criteria missing" in messages["en"]
    assert "Beschreibung und Akzeptanzkriterien fehlen" in messages["de"]


def test_missing_translation_falls_back_and_is_tracked():
    with use_language("de"), track_missing() as missing:
        assert gettext("Not in any catalog") == "Not in any catalog"
        assert ngettext("%(num)d gizmo", "%(num)d gizmos", 2) == "2 gizmos"
    assert missing == {"Not in any catalog", "%(num)d gizmo"}


def test_normalize_language():
    assert i18n.normalize("de-DE") == "de" and i18n.normalize("EN") == "en"
    assert i18n.normalize("fr") is None and i18n.normalize(None) is None


def test_default_language_from_env(monkeypatch):
    monkeypatch.setattr(get_settings(), "ui_language", "en")
    i18n.clear_default_cache()
    assert i18n.get_language() == "en"
    monkeypatch.setattr(get_settings(), "ui_language", "de")
    i18n.clear_default_cache()
    assert i18n.get_language() == "de"
    monkeypatch.setattr(get_settings(), "ui_language", "xx")  # Invalid value → English.
    i18n.clear_default_cache()
    assert i18n.get_language() == "en"


# --- Render every template in both languages -------------------------------------------------------

NOW = datetime(2026, 3, 4, 5, 6, tzinfo=UTC)


def _request(path="/tasks/DEMO-0001", query=b"msg=ok&err=failed"):
    return Request({"type": "http", "method": "GET", "path": path, "query_string": query, "headers": [],
                    "scheme": "http", "server": ("testserver", 80), "root_path": ""})


def _task(status: str, assigned: bool, filled: bool) -> dict:
    sub = {"task_nr": "DEMO-0002", "title": "Subtask", "status": "defined"}
    return {
        "task_nr": "DEMO-0001", "title": "Write release notes", "status": status, "prio": 2, "row_version": 3,
        "assigned_to": "builder" if assigned else None, "due_date": "2026-04-01" if filled else None,
        "description": "Some *text*" if filled else "", "acceptance_criteria": "Done when done" if filled else "",
        "comments": [{"author": "alex", "type": "question", "created_at": NOW, "body": "Why?"}] if filled else [],
        "project": "DEMO", "created_by": "alex", "created_at": NOW, "lease_expires_at": NOW if filled else None,
        "project_info": {"privacy": "local_only" if filled else "cloud_ok", "repo_url": "https://git.example/r",
                         "gitea_owner": "team", "gitea_repo": "demo" if filled else None},
        "branch_name": "task/demo", "pr_url": "https://git.example/pr/1", "pr_number": 1,
        "detail_task_git_url": "https://git.example/spec", "result_commit_sha": "0123456789abcdef",
        "result_summary": "All good", "parent_task_nr": "DEMO-0000" if filled else None,
        "attachments": [{"url": "https://git.example/a", "filename": "mock.png", "size_bytes": 2048,
                         "uploaded_by": "alex", "created_at": NOW}] if filled else [],
        "dependencies": [sub] if filled else [], "subtasks": [sub] if filled else [],
        "follow_up_of": "DEMO-0000" if filled else None, "follow_ups": [sub] if filled else [],
        "open_points": ["Add rate limiting"] if filled else [],
        "events": [{"created_at": NOW, "actor": None, "type": "status", "from": "draft", "to": "defined",
                    "note": "ok"}],
    }


def _contexts(name: str) -> list[dict]:
    """Provide contexts per template to render as many text-bearing branches as possible."""
    user = SimpleNamespace(name="alex", is_admin=True)
    base = {"request": _request(), "user": user, "db_ready": True, "csrf": "tok",
            "qc_projects": [("DEMO", "Demo project")], "error": "Something failed", "message": "All fine",
            "message_kind": "ok", "kind": "ok", "section": "users", "nr": "DEMO-0001"}
    card = {"task_nr": "DEMO-0001", "title": "Card", "prio": 1, "status": "in_progress", "project": "DEMO",
            "assigned_to": "builder", "created_at": NOW, "updated_at": NOW, "pr_url": "https://git.example/pr/1",
            "last_question": "Which **one**?", "result_summary": "Done", "sprint": 7, "model_tier": "simple",
            "model": None}
    owner_ctx = {"owner": "me", "owners": ["alex"], "owner_hint": True}
    locked_ctx = {**owner_ctx, "owner_locked": True}  # Profile switch "Show all projects" off.
    sprint_opts = [{"id": 7, "name": "Sprint A", "status": st, "project": "DEMO"}
                   for st in ("planned", "released", "closed")]
    filters = {"projects": [("DEMO", "Demo project")], "agents": ["builder"], "statuses": list(TASK_STATUSES),
               "sprints": sprint_opts, "sprint_names": {7: "Sprint A"}, "sprint_filter": "7",
               "agent_models": {"builder": {"allowed": ["sonnet"], "applies": False}},
               "reviewers": [{"name": "alex", "kind": "human"}, {"name": "checker", "kind": "agent"}]}
    editor = {"projects": [("DEMO", "Demo project"), ("KET", "Ketchup")], "manage_keys": ["DEMO"],
              "agents": ["builder"], "agent_models": {"builder": {"allowed": [], "applies": True}}, "removed_json": "[]", "error": None, "status_labels": {"draft": "Draft"},
              "entries_json": "[{}]", "items": [
                  {"task_nr": None, "status": "draft", "title": "New", "missing": "Description"},
                  {"task_nr": "DEMO-0001", "status": "draft", "title": "Ready", "missing": ""},
                  {"task_nr": "DEMO-0002", "status": "in_progress", "title": "Running", "missing": ""}]}
    sprint = {"id": 7, "name": "Sprint A", "goal": "Ship it", "start_date": "2026-10-12", "end_date": None,
              "status": "planned", "released_at": NOW, "closed_at": NOW, "task_counts": {"draft": 2, "done": 1}}
    sprint_task = {**card, "status": "draft", "missing": ["description", "acceptance_criteria"]}
    preview = {"released": [{"task_nr": "DEMO-0003", "title": "Ready"}],
               "blocked": [{"task_nr": "DEMO-0001", "title": "Card", "missing": ["description"]}]}
    sprint_page = {"tasks": [sprint_task], "targets": [SimpleNamespace(id=8, name="Sprint B", status="planned")],
                   "agents": ["builder"], "can_plan": True, "can_manage": True, "total": 3, "done": 1,
                   "cancelled": 0, "open_count": 2, "batch_format": "Title | Agent | Prio"}
    project = SimpleNamespace(id=1, project_key="DEMO", name="Demo", description="A demo", owner_id=1,
                              status="idea", privacy="local_only", gitea_url="https://git.example/r",
                              gitea_owner="team", gitea_repo="demo", default_branch="main", brain_path="brain.md")
    owner = SimpleNamespace(name="alex", is_active=False)
    actor = SimpleNamespace(
        name="builder", display_name="Builder", is_active=False, actor_type="human", agent_kind="other",
        role="worker", runtime="local", lease_minutes=60, max_parallel_tasks=1, email="b@example.org",
        tailscale_login="b@github", notes="", last_seen_at=NOW, api_key_hash="x", api_key_prefix="tm_abc",
        api_key_created_at=NOW, gitea_username="builder", gitea_token_last4="9876", gitea_email=None,
        gitea_token_checked_at=NOW, id=1)
    member = SimpleNamespace(gitea_permission="write", sync_error="boom", gitea_synced_at=NOW)
    log_row = SimpleNamespace(created_at=NOW, event="review", task_nr="DEMO-0001", status="sent", attempts=1,
                              last_error="Telegram 403: blocked")
    profile = {"actor": SimpleNamespace(**{**vars(actor), "role": "admin"}), "bot_enabled": True,
               "bot_username": None, "link": None, "pending": None, "events": {"review": "Review"},
               "enabled": {"review"}, "quiet_start": "22:00", "quiet_end": "07:00", "all_projects": True,
               "mute_all": None, "mute_review": None, "forever": FOREVER, "log": [log_row],
               "status_labels": {"sent": "sent"}, "mute_options": [("keep", "unchanged")]}
    probe = SimpleNamespace(ok=True, server_version="10.5", skip_locked=False, current_revision=None,
                            head_revision="0009", schema_current=False, error="refused")
    setting = {"value": "x", "is_set": True, "source": "db", "choices": []}
    ctxs = {
        "base.html": [{}, {"user": None, "db_ready": False}],
        "_backlog_table.html": [{"items": [card], "total": 1, "params": {"sort": "prio", "desc": True},
                                 "sort_url": lambda col: "/backlog"},
                                {"items": [], "total": 0, "params": {"sort": "prio", "desc": False},
                                 "sort_url": lambda col: "/backlog"}],
        "backlog.html": [{**owner_ctx, **filters, "items": [card], "total": 2, "sort_url": lambda col: "/backlog",
                          "params": {"sort": "title", "desc": False, "status": ["draft"], "prio": [1],
                                     "assigned_to": "none", "project": "DEMO", "q": "x"}},
                         {**owner_ctx, **filters, "items": [], "total": 0, "sort_url": lambda col: "/backlog",
                          "params": {"sort": "prio", "desc": False, "status": [], "prio": []}},
                         {**locked_ctx, **filters, "items": [], "total": 0, "sort_url": lambda col: "/backlog",
                          "params": {"sort": "prio", "desc": False, "status": [], "prio": []}}],
        "kanban.html": [{**c, **filters, **d, "can_drag": True, "agent": "none", "done_ranges": DONE_RANGES,
                         "done_backlog_url": "/backlog?status=done",
                         "columns": _kanban_columns([{**card, "status": s} for s in TASK_STATUSES],
                                                    [{**card, "status": "done"}])}
                        for c, d in ((owner_ctx, {"done_range": "3d", "done_total": 1, "done_all": 1}),
                                     (locked_ctx, {"done_range": "all", "done_total": 120, "done_all": 120}),
                                     (owner_ctx, {"done_range": "today", "done_total": 1, "done_all": 5}))],
        "_card.html": [{"t": {**card, "status": s}} for s in ("defined", "questions", "aborted")],
        "_card_moves.html": [{"nr": "DEMO-0001", "status": "for_review", "targets": ["done", "aborted"]},
                             {"nr": "DEMO-0001", "status": "done", "targets": []}],
        "_status.html": [{"status": s} for s in TASK_STATUSES],
        "_tier.html": [{"t": card}, {"t": {**card, "model_tier": None, "model": "opus"}}],
        "_model_fields.html": [{**filters, "mf": {"model_tier": "complex", "model": "opus"}}, {**filters, "mf": {}}],
        "_reviewer_field.html": [{**filters, "reviewer_value": "checker", "reviewer_default": True},
                                 {**filters, "reviewer_value": "gone", "reviewer_locked": True}],
        "_owner_filter.html": [owner_ctx],
        "_own_projects_hint.html": [{}, {"user": SimpleNamespace(name="alex", is_admin=False, role="viewer")}],
        "_sprint_filter.html": [filters, {**filters, "sprint_filter": "none"}],
        "_sprint_hint.html": [{"t": card, "sprint_names": {7: "Sprint A"}}],
        "sprint.html": [{**c, "p": project} for c in (
            {**sprint_page, "sprint": sprint, "preview": preview, "batch_lines": "a\nb | x | 9",
             "batch_rows": [{"n": 1, "title": "a", "assigned_to": None, "prio": 3, "error": None},
                            {"n": 2, "title": "b", "assigned_to": "x", "prio": 3, "error": "Unknown actor"}],
             "batch_error": "Fix it"},
            {**sprint_page, "sprint": sprint, "preview": {"released": [], "blocked": []},
             "batch_rows": [{"n": 1, "title": "a", "assigned_to": None, "prio": 3, "error": None}]},
            {**sprint_page, "sprint": {**sprint, "status": "released"}, "preview": preview},
            {**sprint_page, "sprint": {**sprint, "status": "released"}, "preview": preview, "targets": [],
             "open_count": 0},
            {**sprint_page, "sprint": {**sprint, "status": "closed", "task_counts": {}}, "preview": None,
             "tasks": [], "total": 0})],
        "sprint_editor.html": [
            {**editor, "sprint": None, "key": None, "items": [], "entries_json": "[]", "error": "Boom",
             "form": {"project": "", "name": "", "goal": "", "start_date": "", "end_date": ""}},
            {**editor, "sprint": {**sprint, "project": "DEMO"}, "key": "DEMO", "manage_keys": [],
             "form": {"name": "Sprint A", "goal": "", "start_date": "", "end_date": ""}},
            {**editor, "sprint": None, "key": None, "items": [], "projects": [], "owner_hint": True,
             "form": {"project": "", "name": "", "goal": "", "start_date": "", "end_date": ""}}],
        "_owner_hint.html": [owner_ctx, locked_ctx],
        "_md_field.html": [{"md_label": "Label", "md_name": "description", "md_value": "x"}],
        "inbox.html": [{**owner_ctx, "managed": ["DEMO"],
                        "inbox": {k: [{**card, "reviewer": "checker", "review_claimed": True}]
                                  for k in ("questions", "unassigned", "aborted", "for_review", "lease_expired",
                                            "agent_review")}},
                       {**locked_ctx, "managed": [],
                        "inbox": {k: [] for k in ("questions", "unassigned", "aborted", "for_review",
                                                  "lease_expired", "agent_review")}}],
        "projects.html": [{"items": [project], "counts": {1: {"defined": 2, "questions": 1}}, "owners": {1: owner},
                           "can_create": True},
                          {"items": [], "counts": {}, "owners": {}, "can_create": False},
                          {"items": [project], "counts": {}, "owners": {1: owner}, "can_create": True,
                           "own_only": True},
                          {"items": [], "counts": {}, "owners": {}, "can_create": True, "own_only": True}],
        "project_new.html": [{"humans": ["alex"], "form": {"gitea_mode": m, "status": "active",
                                                           "privacy": "local_only", "owner": "alex"}}
                             for m in ("create", "link", "none")],
        "project_detail.html": [{"p": project, "info": {"clone_url": "ssh://x", "default_reviewer": "gone"},
                                 "reviewers": filters["reviewers"], "counts": {"defined": 2},
                                 "members": [(member, actor)], "missing": ["brain.md"], "gitea_error": "down",
                                 "can_manage": True, "humans": ["alex"], "owner": owner, "can_plan": True,
                                 "sprints": [SimpleNamespace(id=7, name="Sprint A", status="released",
                                                             start_date=None, end_date="2026-10-23")],
                                 "sprint_counts": {7: {"draft": 1}}},
                                {"p": SimpleNamespace(**{**vars(project), "gitea_repo": None}), "info": {},
                                 "counts": {}, "members": [], "missing": [], "gitea_error": None,
                                 "can_manage": True, "humans": [], "owner": None},
                                {"p": project, "info": {"clone_url": "x"}, "counts": {}, "members": [],
                                 "missing": [], "gitea_error": None, "can_manage": False, "humans": [],
                                 "owner": owner}],
        "task_new.html": [{**filters, "form": {"status": "defined", "prio": "2", "sprint": "7"}},
                          {**filters, "projects": [], "own_projects_only": True, "form": {"status": "draft", "prio": "3"}},
                          {**filters, "sprints": [], "back": "/backlog", "error": "Boom", "error_section": "links",
                           "form": {"status": "draft", "prio": 3, "model_tier": "complex", "model": "opus",
                                    "parent_task_nr": "DEMO-0001", "create_spec_file": True}}],
        "task_follow_up.html": [{**filters, "origin": card, "form": {
            "title": "Follow-up", "description": "", "acceptance_criteria": "", "prio": 3, "status": status,
            "assigned_to": "", "points": points, "selected": [0], "points_into": into}}
            for status, points, into in (("draft", ["One", "Two"], "description"),
                                         ("defined", ["One"], "acceptance_criteria"), ("draft", [], ""))],
        "error.html": [{}],
        "login.html": [{"user": None, "username": "alex"}],
        "_flash.html": [{}],
        "_merge_conflict.html": [{}],
        "_logo.html": [{}],
        "admin/_nav.html": [{}],
        "admin/database.html": [{"cfg": {"host": "db", "port": 3306, "database": "tm", "user": "tm",
                                         "password_set": s}, "probe": probe} for s in (True, False)],
        "admin/_db_status.html": [{"probe": probe},
                                  {"probe": SimpleNamespace(**{**vars(probe), "ok": False})},
                                  {"probe": SimpleNamespace(**{**vars(probe), "skip_locked": True,
                                                               "schema_current": True})}],
        "admin/_sftp_test.html": [{"fingerprint": "SHA256:" + "A" * 43, "stored": stored, "matches": matches}
                                  for stored, matches in (("", False), ("SHA256:" + "A" * 43, True),
                                                          ("SHA256:" + "B" * 43, False))],
        "admin/_db_test_result.html": [{"result": probe}, {"result": SimpleNamespace(**{**vars(probe), "ok": False})}],
        "admin/settings.html": [{"settings": {
            "ui_language": {**setting, "label": "Default UI language", "kind": "choice", "secret": False,
                            "choices": ["en", "de"], "value": "de"},
            "merge_style": {**setting, "label": "Merge style", "kind": "choice", "secret": False,
                            "choices": ["squash"]},
            "branch_protection": {**setting, "label": "Branch protection for new repos", "kind": "bool",
                                  "secret": False, "value": "true"},
            "gitea_token": {**setting, "label": "Gitea service token", "kind": "str", "secret": True},
            "telegram_bot_token": {**setting, "label": "Telegram bot token (from @BotFather)", "kind": "str",
                                   "secret": True, "is_set": False, "source": None},
            "default_lease_minutes": {**setting, "label": "Default lease (minutes)", "kind": "int",
                                      "secret": False}},
            "notification_log": [(log_row, owner), (log_row, None)], "events": {"review": "Review"},
            "status_labels": {"sent": "sent"}}],
        "profile.html": [{**profile, "link": SimpleNamespace(chat_name="alex", chat_id=1, linked_at=NOW)},
                         {**profile, "bot_enabled": False},
                         {**profile, "code": "abc", "code_expires": NOW, "bot_username": "tb_bot",
                          "pending": SimpleNamespace(expires_at=NOW)},
                         {**profile, "pending": SimpleNamespace(expires_at=NOW),
                          "mute_all": NOW.replace(tzinfo=None), "mute_review": FOREVER}],
        "admin/users.html": [{"actors": [actor, SimpleNamespace(**{**vars(actor), "actor_type": "agent",
                                                                   "is_active": True, "api_key_hash": None,
                                                                   "gitea_username": None})],
                              "counts": {1: {"current": "DEMO-0001", "open": 2}}, "owners": {1: "alex"},
                              "filters": f, "filtered": any(f.values()), "total": 3, "owner_choices": ["alex"],
                              "roles": ["worker"], "kinds": ["other"]}
                             for f in ({"owner": "", "role": "", "kind": ""},
                                       {"owner": "none", "role": "worker", "kind": "other"})],
        "admin/user_form.html": [
            {"actor": actor, "form": {}, "kinds": ["other"], "roles": ["worker"], "counts": {"open": 1, "current": "X"},
             "access": [{"project": project, "member": member, "blocked": True},
                        {"project": project, "member": SimpleNamespace(**{**vars(member), "sync_error": None}),
                         "blocked": False},
                        {"project": project, "member": SimpleNamespace(gitea_permission="read", sync_error=None,
                                                                       gitea_synced_at=None), "blocked": False}]},
            {"actor": SimpleNamespace(**{**vars(actor), "is_active": True, "api_key_hash": None,
                                         "gitea_username": None}),
             "form": {}, "kinds": ["other"], "roles": ["worker"], "counts": {"open": 0}, "access": []},
            {"actor": None, "form": {"actor_type": "human"}, "kinds": ["other"], "roles": ["worker"]},
            *({"actor": SimpleNamespace(**{**vars(actor), "actor_type": "agent", "agent_kind": "claude_code"}),
               "form": {}, "kinds": ["other"], "roles": ["worker"], "counts": {"open": 0}, "access": [],
               "models": {"default": "sonnet", "tiers": {"simple": "haiku"}, "allowed": ["opus"], "applies": applies},
               "model_tiers": {"simple": "simple", "standard": "standard", "complex": "complex"},
               "sftp_enabled": applies}
              for applies in (True, False)),
            {"actor": SimpleNamespace(**{**vars(actor), "actor_type": "agent", "agent_kind": "codex",
                                         "sftp_username": "bot", "sftp_password_last4": "1234", "sftp_checked_at": None}),
             "form": {}, "kinds": ["other"], "roles": ["worker"], "counts": {"open": 0}, "access": [],
             "models": {"default": None, "tiers": {}, "allowed": [], "applies": True}, "model_tiers": {},
             "sftp_enabled": True}],
        "admin/user_token.html": [{"actor": actor, "token": "tm_x", "base_url": "http://x", "created": c}
                                  for c in (True, False)],
        "admin/agent_new.html": [{"form": {"owner": "alex", "is_public": auto}, "kinds": ["other"], "roles": ["worker"],
                                  "projects": [project], "gitea_auto": auto, "humans": ["alex"]}
                                 for auto in (True, False)],
        "admin/agent_setup.html": [
            {"actor": SimpleNamespace(**{**vars(actor), "actor_type": "agent", "is_active": True}), "created": True,
             "code": "tmj_x", "expires": NOW, "commands": {"join": "tm join", "install": "curl"}, "pending": None,
             "gitea_auto": True, "gitea_configured": True, "join_minutes": 15, "gitea_error": None},
            *({"actor": SimpleNamespace(**{**vars(actor), "actor_type": "agent", "is_active": True,
                                           "gitea_username": None}),
               "code": None, "pending": SimpleNamespace(expires_at=NOW), "gitea_auto": auto,
               "gitea_configured": configured, "join_minutes": 15, "gitea_error": "boom"}
              for auto in (True, False) for configured in (True, False))],
    }
    if name == "task_detail.html":
        task_ctx = {**filters, "me": SimpleNamespace(name="alex"), "merge_conflict": True}
        variants = []
        for status in TASK_STATUSES:
            for assigned in (True, False):
                for filled in (True, False):
                    variants.append({**task_ctx, "t": _task(status, assigned, filled), "can_manage": filled,
                                     "can_review": True, "can_answer": True, "human_admin": True})
        variants.append({**task_ctx, "t": {**_task("draft", True, False), "sprint": 7, "sprint_name": "Sprint A",
                                           "sprint_status": "planned"},
                         "project_sprints": filters["sprints"], "can_manage": True, "can_review": False,
                         "human_admin": False})
        variants.append({**task_ctx, "t": {**_task("draft", True, True), "description": ""},
                         "can_manage": True, "can_review": False, "human_admin": False})
        variants.append({**task_ctx, "t": {**_task("draft", True, True), "acceptance_criteria": ""},
                         "can_manage": True, "can_review": False, "human_admin": False})
        # Error re-render: edit form open with the entered values, thread tab with the unsent comment.
        variants.append({**task_ctx, "t": _task("defined", True, True), "can_manage": True, "can_review": False,
                         "human_admin": False, "edit_form": {"title": "Changed", "prio": 2},
                         "comment_body": "Unsent"})
        review = {**_task("for_review", True, True), "reviewer": "checker", "reviewer_kind": "agent",
                  "reviewer_effective": False,
                  "comments": [{"author": "checker", "type": "review", "created_at": NOW, "body": "OK",
                                "by_agent": True}]}
        for claimed in (NOW, None):
            variants.append({**task_ctx, "t": {**review, "review_claimed_at": claimed}, "agent_reviewer": "checker",
                             "can_manage": True, "can_review": True, "can_answer": True, "human_admin": True})
        for applies in (True, False):
            variants.append({**task_ctx, "t": {**_task("defined", True, True), "model_tier": "complex", "model": None,
                                               "model_effective": {"model": "opus", "source": "tier",
                                                                   "applies": applies}},
                             "can_manage": True, "can_review": False, "human_admin": False})
        return [{**base, **v} for v in variants]
    assert name in ctxs, f"Kein Testkontext für {name} – bitte in _contexts() ergänzen"
    return [{**base, **c} for c in ctxs[name]]


@pytest.mark.parametrize("name", [n for n in UI_TEMPLATES if not n.startswith("repo/")])
@pytest.mark.parametrize("lang", list(i18n.LANGUAGES))
def test_all_templates_render_without_missing_translations(name, lang):
    template = templates.get_template(name)
    with use_language(lang), track_missing() as missing:
        pages = [template.render(ctx) for ctx in _contexts(name)]
    assert not missing, f"{name} ({lang}): fehlende Übersetzungen {sorted(missing)}"
    assert all(pages)
    if lang == "en":
        leftovers = sorted({t for page in pages for t in _german_leftovers(page)})
        assert not leftovers, f"{name}: nicht übersetzte (deutsche) Texte in der englischen Ansicht: {leftovers}"


# Common German words/characters in an English view indicate a missing _() in the template.
GERMAN = re.compile(r"[äöüÄÖÜß„]|\b(und|der|die|das|nicht|noch|keine?|für|mit|oder|bitte|zurück|neue[rs]?|"
                    r"Speichern|Anlegen|Projekte?|Zugewiesen|Fällig|Beschreibung|Erstellt|Abbrechen|Freigeben|"
                    r"Entwurf|Rückfragen?|Benutzer|Passwort|Anmelden|Einstellungen|Datenbank|Fehler|Status setzen)\b")


class _Texts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.texts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "code", "pre"):
            self._skip += 1
        self.texts += [v for k, v in attrs if k in ("title", "placeholder", "aria-label", "hx-confirm",
                                                    "onclick", "onsubmit") and v]

    def handle_endtag(self, tag):
        if tag in ("script", "style", "code", "pre"):
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.texts.append(data.strip())


def _german_leftovers(html: str) -> list[str]:
    parser = _Texts()
    parser.feed(html)
    return [t for t in parser.texts if GERMAN.search(t)]


def test_leftover_detector_finds_german():
    assert _german_leftovers('<p title="Speichern">Save</p><button>Abbrechen</button>') == ["Speichern", "Abbrechen"]
    assert _german_leftovers("<p>Save the task</p><code>für</code>") == []


# --- Request language selection ------------------------------------------------------------------------------

@pytest.fixture
def english_default(monkeypatch):
    monkeypatch.setattr(get_settings(), "ui_language", "en")
    i18n.clear_default_cache()


def test_default_language_is_english(client, english_default):
    html = client.get("/login").text
    assert '<html lang="en" ' in html and ">Log in</h2>" in html and "Anmelden" not in html
    assert 'href="/language?lang=de&amp;next=/login"' in html


def test_configured_german(client):  # conftest: UI_LANGUAGE=de
    html = client.get("/login").text
    assert '<html lang="de" ' in html and ">Anmelden</h2>" in html


def test_language_switch_sets_cookie_and_redirects(client, english_default):
    r = client.get("/language", params={"lang": "de", "next": "/login?x=1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?x=1"
    assert r.cookies.get(i18n.LANG_COOKIE) == "de"
    assert ">Anmelden</h2>" in client.get("/login").text
    client.get("/language", params={"lang": "en"})
    assert ">Log in</h2>" in client.get("/login").text


@pytest.mark.parametrize("target", ["//evil.example/x", "https://evil.example", "/\\evil.example"])
def test_language_switch_rejects_foreign_targets(client, target):
    r = client.get("/language", params={"lang": "de", "next": target}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_language_switch_unknown_resets(client, english_default):
    client.cookies.set(i18n.LANG_COOKIE, "de")
    r = client.get("/language", params={"lang": "default"}, follow_redirects=False)
    assert i18n.LANG_COOKIE in r.headers.get("set-cookie", "")  # Cookie is deleted.
    client.cookies.clear()
    assert ">Log in</h2>" in client.get("/login").text


def test_invalid_cookie_falls_back_to_default(client, english_default):
    client.cookies.set(i18n.LANG_COOKIE, "xx")
    assert ">Log in</h2>" in client.get("/login").text


def test_login_error_translated(client, english_default):
    csrf = client.get("/login").text.split('name="csrf_token" value="')[1].split('"')[0]
    r = client.post("/login", data={"username": "admin", "password": "wrong", "csrf_token": csrf})
    assert r.status_code == 401 and "Wrong username or password." in r.text


@pytest.mark.db
def test_admin_setting_overrides_env_default(ready_db, client):
    from app.services import settings_store

    with ready_db.session() as s:
        settings_store.update(s, {"ui_language": "en"}, None)
        s.commit()
    assert ">Log in</h2>" in client.get("/login").text  # .env says de, admin setting says en.
    client.cookies.set(i18n.LANG_COOKIE, "de")
    assert ">Anmelden</h2>" in client.get("/login").text  # Personal choice overrides the setting.
    with ready_db.session() as s:
        with pytest.raises(Exception, match="en, de"):
            settings_store.update(s, {"ui_language": "fr"}, None)


@pytest.mark.db
def test_pages_in_english_with_db(ready_db, client):
    from tests.conftest import login

    client.cookies.set(i18n.LANG_COOKIE, "en")
    assert login(client).status_code == 303
    csrf = client.get("/projects/new").text.split('name="csrf_token" value="')[1].split('"')[0]
    r = client.post("/projects/new", data={"csrf_token": csrf, "key": "DEMO", "name": "Demo", "status": "idea",
                                           "privacy": "local_only", "gitea_mode": "none", "owner": "admin"})
    assert r.status_code == 200 and "Project created" in r.text, r.text[:500]
    r = client.post("/tasks/new", data={"csrf_token": csrf, "project": "DEMO", "title": "Hello", "prio": "2",
                                        "status": "draft"})
    assert r.status_code == 200 and "Not ready for release yet" in r.text
    for url in ("/", "/backlog", "/kanban", "/projects", "/projects/DEMO", "/tasks/new", "/projects/new",
                "/tasks/DEMO-0001", "/admin/users", "/admin/users/admin", "/admin/users/new", "/admin/settings",
                "/admin/database"):
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:300])
        assert '<html lang="en" ' in r.text and not _german_leftovers(r.text), (url, _german_leftovers(r.text))
