"""Translation layer: complete catalog, templates render in both languages, language selection."""
import ast
import re
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app import i18n
from app.config import get_settings
from app.i18n import catalog, gettext, ngettext, track_missing, use_language
from app.models import PROJECT_STATUSES, TASK_STATUSES
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
    assert "Log in" in MSGIDS and STATUS_LABELS["in_progress"] in MSGIDS and MSGIDS["%(num)d sprint"][1]


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
        assert gettext("Log out") == "Log out"
        assert ngettext("%(num)d task", "%(num)d tasks", 1) == "1 task"
        assert ngettext("%(num)d task", "%(num)d tasks", 0) == "0 tasks"
        assert status_label("in_progress") == "In progress" and status_label("weird") == "weird"
    with use_language("de"):
        assert gettext("Saved.") == "Gespeichert."
        assert gettext("Log out") == "Abmelden"
        assert ngettext("%(num)d sprint", "%(num)d sprints", 1) == "1 Sprint"
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
MAIL_MODES = {"freigabe": "with approval", "ohne_freigabe": "without approval"}
MAIL = {"id": 1, "task_nr": "DEMO-0001", "status": "pending_approval", "status_label": "waiting for approval",
        "to": ["client@example.org"], "cc": ["team@example.org"], "reply_to": None, "subject": "Result",
        "body": "Hello", "attachments": [{"ref": "branch:out/r.pdf", "name": "r.pdf", "size": 2048,
                                          "sha256": "a" * 64, "content_type": "application/pdf"}],
        "on_approve": True, "requested_by": "claude-code", "approved_by": None, "approved_at": None,
        "decided_note": None, "message_id": None, "smtp_error": None, "attempts": 0, "created_at": NOW,
        "sent_at": None, "cleared": False}


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
        "data_class": "sealed" if filled else None, "data_class_effective": "sealed" if filled else "cloud_ok",
        "task_kind": "run" if filled else "work",
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


def _data_contexts(project) -> dict:
    """Menu "Data" (app/web/data_ui.py): list, upload form and dataset detail."""
    from datetime import date, timedelta

    from app.services import datasets

    labels = {"status_labels": dict(datasets.STATUS_LABELS), "class_labels": dict(datasets.CLASS_LABELS),
              "classes": ["local_only", "sealed"], "privacy": _privacy_context(), "can_configure_policy": True}
    ds = SimpleNamespace(id=3, dataset_key="DS-0003", name="Bookings", project_id=1, data_class="sealed",
                         status="quarantine", size_bytes=2400000, original_filename="b.csv", media_type="text/csv",
                         sha256="ab" * 32, created_at=NOW, uploaded_by=1, upload_channel="web",
                         retention_until=date(2027, 1, 14), min_group_size=None, deleted_at=None, deleted_by=None,
                         supersedes_id=2)
    gone = SimpleNamespace(**{**vars(ds), "id": 2, "dataset_key": "DS-0002", "status": "deleted", "deleted_at": NOW,
                              "deleted_by": None, "retention_until": date.today() - timedelta(days=1)})
    soon = SimpleNamespace(**{**vars(ds), "id": 4, "dataset_key": "DS-0004", "data_class": "local_only"})
    entry = SimpleNamespace(id=1, at=NOW, action="dataset_uploaded", actor_id=1, via="web")
    listing = {"projects": [project], "project_keys": {1: "DEMO"}, "warn_days": 7, **labels,
               "f": {"project": "DEMO", "status": "live", "data_class": "sealed", "q": "x"}}
    return {
        "data_list.html": [
            {**listing, "rows": [ds, gone, soon], "retention_left": {3: 90, 2: -1, 4: 3}, "can_upload": True,
             "problem": None, "enabled": True},
            {**listing, "rows": [], "retention_left": {}, "can_upload": False, "problem": "Switched off",
             "enabled": False}],
        "data_upload.html": [
            {**labels, "projects": [project], "replaceable": [ds], "project_keys": {1: "DEMO"},
             "values": {"project": "DEMO", "name": "x", "data_class": "local_only"}, "problem": None, "max_mb": 200,
             "default_retention": 90},
            {**labels, "projects": [], "replaceable": [], "project_keys": {}, "values": {}, "problem": None,
             "max_mb": 200, "default_retention": 90},
            {**labels, "projects": [project], "replaceable": [], "project_keys": {}, "values": {},
             "problem": "Key missing", "max_mb": 200, "default_retention": 90}],
        "data_detail.html": [
            {**labels, "ds": ds, "project": project, "entries": [entry], "details": {1: {"size_bytes": 3}},
             "actor_names": {1: "alex"}, "names": {1: "alex"}, "replaced": gone, "can_delete": True,
             "can_download": True, "retention_left": 90, "message": "Stored",
             "runs": [{"id": 12, "task_nr": "DEMO-0003", "status": "released", "label": "Released",
                       "finished_at": NOW, "script": "analysis/x.py", "commit": "a1b2c3d4e5"}]},
            {**labels, "ds": gone, "project": project, "entries": [], "details": {}, "actor_names": {},
             "names": {}, "replaced": None, "can_delete": False, "can_download": False, "retention_left": -1,
             "message": None}],
        "data_result.html": _gate_contexts(project, ds),
    }


def _gate_contexts(project, ds) -> list[dict]:
    """Release view of the output gate (app/services/output_gate.view): pending with warnings and helper columns,
    four-eyes blocked, released, unreadable."""
    result = SimpleNamespace(id=12, status="pending_approval", script_path="analysis/x.py",
                             script_commit_sha="a1" * 20, exit_code=0, peak_memory_mb=610, finished_at=NOW)
    task = SimpleNamespace(task_nr="DEMO-0003")
    code = SimpleNamespace(task_nr="DEMO-0002", pr_url="http://x/pr/41", pr_number=41)
    checks = [{"n": 1, "key": "format", "kind": "hard", "status": "ok", "label": "Format", "text": "2 tables"},
              {"n": 6, "key": "free_text", "kind": "warning", "status": "warning", "label": "Free text",
               "text": "1 long text", "where": ["t0.r0.c0", "t0.col1"], "count": 14},
              {"n": 7, "key": "dominance", "kind": "warning", "status": "not_run", "label": "Dominance", "text": ""}]
    doc = {"format": "tbr-aggregate/1", "metrics": {"rows": 3},
           "tables": [{"name": "per_month", "columns": ["month", "n", "total", "top"], "count_column": "n",
                       "max_contribution": {"total": "top"}, "rows": [["2026-01", 5, 10.5, 2.0], [None, 7, 1, 1]]}]}
    base = {"result": result, "task": task, "project": project, "dataset": ds, "doc": doc, "doc_error": None,
            "privacy": _privacy_context(),
            "release_policy": {"mechanism": "laplace-4u/1", "epsilon": "0.1", "sensitivity": "2"},
            "hits": {"t0.r0.c0", "t0.col1", "t0.name", "m0"}, "logs": {"stdout.log": {"text": "x", "truncated": True},
                                                                      "stderr.log": {"text": "", "truncated": False}},
            "checks": checks, "summary": {"k": 5, "failed": [], "warnings": [6]}, "code_task": code,
            "merger": "alex", "diff": "http://x/compare/a...b", "previous": None, "runtime": 38,
            "helper_columns": ["top"], "status_label": "Awaits release", "message": "Done", "error": "Boom",
            "entries": [SimpleNamespace(id=1, at=NOW, action="gate_checked", actor_id=None, via="runner")],
            "details": {}, "actor_names": {},
            "approvals": [{"actor": "bob", "at": "2026-10-16T10:00:00", "decision": "reject", "note": "No"},
                          {"actor": None, "at": "2026-10-16T11:00:00", "decision": "keep_sealed", "note": ""}],
            "options": {"release": True, "reject": True, "keep_sealed": True, "four_eyes_blocked": False,
                        "four_eyes": True}}
    released = SimpleNamespace(**{**vars(result), "status": "released"})
    return [base,
            {**base, "options": {"release": False, "reject": True, "keep_sealed": True, "four_eyes_blocked": True,
                                 "four_eyes": True}, "diff": None, "previous": result, "code_task": None},
            {**base, "result": released, "options": {k: False for k in base["options"]}, "approvals": [],
             "message": None, "error": None, "runtime": None, "dataset": None},
            {**base, "doc": None, "doc_error": "result.json could not be decrypted", "checks": [], "logs": {},
             "entries": []}]


def _privacy_context():
    return {"released_count": 2, "query_limit": 2, "epsilon_spent": "0.2", "epsilon_limit": "0.2",
            "noise": True, "epsilon": "0.1", "sensitivity": "2", "token": "test-token", "exhausted": True}


def _workflow_contexts(project, card) -> dict:
    """Menu "Workflows" (app/web/workflows_ui.py): list, new, template page, diff, editor, history, runs, run."""
    from app.services.workflows import starters, ui

    version = SimpleNamespace(id=2, version=2, state="published", published_at=NOW, change_note="Reordered",
                              steps=[], params=[])
    draft = SimpleNamespace(**{**vars(version), "id": 3, "version": 3, "state": "draft", "published_at": None})
    template = SimpleNamespace(id=1, name="Monthly report", workflow_key="monthly", project=project, status="paused",
                               description="Report *monthly*", current_version=version, current_version_id=2,
                               owner_id=1, default_budget_tokens=None, default_budget_cents=300)
    run = SimpleNamespace(id=7, run_seq=7, status="paused", trigger_kind="manual", version=version, template=template,
                          project=project, started_at=NOW, created_at=NOW, finished_at=NOW, scheduled_for=NOW,
                          started_by=1, pause_reason="no_assignee")
    cell = {"key": "write", "type": "agent", "position": 1, "attempt": 2, "status": "succeeded", "symbol": "✓",
            "task_nr": "DEMO-0001", "task_status": "done", "failed_before": True, "repaired": True,
            "error_code": None, "duration": 3700, "data_class": "local_only", "current": False}
    chain = [cell, {**cell, "key": "approve", "type": "approval", "status": "waiting", "symbol": "◷",
                    "repaired": False, "attempt": 1, "duration": None, "current": True, "error_code": "x"},
             {**cell, "key": "inform", "type": "notify", "status": "pending", "symbol": "○", "task_nr": None,
              "repaired": False, "attempt": 0, "duration": None}]
    info = {"name": "Monthly report", "description": "x", "params": [
        {"name": "topic", "type": "string", "required": True, "default": "x", "values": None, "label": "Topic"},
        {"name": "flag", "type": "bool", "required": False, "default": None, "values": None, "label": None},
        {"name": "kind", "type": "enum", "required": True, "default": None, "values": ["a", "b"], "label": "***"},
        {"name": "month", "type": "month", "required": True, "default": None, "values": None, "label": "M"}],
        "steps": [{"key": "write", "type": "agent", "data_class": None, "role": "writer", "review": "agent",
                   "title": "Write", "masked": False},
                  {"key": "secret", "type": "approval", "data_class": "sealed", "role": None, "review": "manual",
                   "title": "***", "masked": True}]}
    values = {"name": "Monthly report", "description": "x", "change_note": "y",
              "params": [{"name": "topic", "type": "string", "required": "1", "default": "", "values": "",
                          "label": "Topic", "help": ""}],
              "steps": [{"key": "write", "type": "agent", "title": "Write", "description": "", "acceptance": "",
                         "assignee": "", "role": "writer", "review": "manual", "reviewer": "", "data_class": "",
                         "model_tier": "", "timeout": "", "retries": "0", "optional": "", "outputs": "{}",
                         "inputs": "", "config": ""}]}
    issues = [{"code": "unknown_placeholder", "label": "Unknown", "message": "x", "severity": "error", "step_key": "a"},
              {"code": "role_unresolvable", "label": "Role", "message": "y", "severity": "warning", "step_key": None}]
    labels_v = [{"version": draft, "runs": 0, "publisher": None, "current": False},
                {"version": version, "runs": 3, "publisher": "alex", "current": True}]
    row = {"template": template, "project": "DEMO", "key": "monthly", "steps": 3, "version": 2, "draft": True,
           "runs": [run], "last": run, "can_edit": True, "can_start": True, "start_params": info["params"],
           "impact": {"runs": 1, "active": 1, "schedules": 2, "triggers": 1}, "start_dialog": True}
    attempt = {"key": "write", "type": "agent", "position": 1, "attempt": 1, "status": "failed",
               "task_nr": "DEMO-0001", "task_kind": "run", "task_status": "done", "data_class": "cloud_ok",
               "masked": False, "outputs": {"output": {"report": "r.md", "n": 3}, "task_nr": "DEMO-0001", "pr": 4},
               "error_code": "no_assignee", "error_detail": "None",
               "row": SimpleNamespace(activated_at=NOW, finished_at=NOW)}
    run_info = {"params": {"topic": "x"}, "error_detail": "Check", "used_cents": 0, "budget_cents": 300,
                "used_tokens": 0, "budget_tokens": None, "usage_reported": True}
    detail = {"template": template, "info": info, "selected": version, "versions": labels_v, "draft": draft,
              "can_edit": True, "can_start": True, "start_params": info["params"], "recent": [run],
              "base": "/workflows/DEMO/monthly"}
    return {
        "_workflow_actions.html": [{"r": row}, {"r": {**row, "can_edit": False, "can_start": False}}],
        "workflows.html": [{"rows": [row, {**row, "version": None, "runs": [], "last": None, "draft": False}],
                            "project": "DEMO", "projects": ["DEMO"], "can_create": True, "q": "", "filtered": False,
                            "starters": starters.STARTERS, "wf_counts": {"templates": 2, "runs": 1, "schedules": 0,
                                                                         "triggers": 0}},
                           {"rows": [], "project": "", "projects": [], "can_create": True, "q": "x", "filtered": True,
                            "starters": starters.STARTERS},
                           {"rows": [], "project": "", "projects": [], "can_create": True, "q": "", "filtered": False,
                            "starters": starters.STARTERS}],
        "workflow_new.html": [{"choices": ["DEMO"], "form": {"project": "DEMO", "key": "", "name": "", "starter": ""}},
                              {"choices": ["DEMO"], "starter": starters.STARTERS["schedule"],
                               "form": {"project": "DEMO", "key": "k", "name": "n", "starter": "schedule"}}],
        "workflow_detail.html": [detail, {**detail, "info": None, "selected": None, "versions": [], "recent": [],
                                          "can_start": False}],
        "workflow_diff.html": [{"template": template, "old": version, "new": draft, "choices": ["draft", "2"],
                                "base": "/workflows/DEMO/monthly",
                                "rows": [{"kind": k, "text": "x"} for k in ("hunk", "ctx", "add", "del")]},
                               {"template": template, "old": None, "new": version, "choices": [], "rows": [],
                                "base": "/workflows/DEMO/monthly"}],
        "workflow_editor.html": [{"template": template, "values": values, "draft": d, "issues": issues,
                                  "actors": [SimpleNamespace(id=1, name="alex")], "data_classes": ("cloud_ok", "local_only", "sealed"),
                                  "model_tiers": ("simple", "complex"),
                                  "step_types": ui.STEP_TYPES, "param_types": ui.PARAM_TYPES,
                                  "placeholder_data": "{}", "base": "/workflows/DEMO/monthly"}
                                 for d in (draft, None)],
        "_workflow_checks.html": [{"issues": issues}, {"issues": []}],
        "workflow_history.html": [{"template": template, "runs": [run], "cells": {7: {"write": cell}},
                                   "columns": ["write", "old"], "status": "paused", "trigger": "manual",
                                   "statuses": tuple(ui.RUN_STATUS_LABELS), "base": "/workflows/DEMO/monthly",
                                   "params": {7: {"topic": "x"}}},
                                  {"template": template, "runs": [run], "cells": {}, "columns": [], "status": "",
                                   "trigger": "", "statuses": (), "base": "/w", "params": {7: None}},
                                  {"template": template, "runs": [], "cells": {}, "columns": [], "status": "",
                                   "trigger": "", "statuses": (), "base": "/w", "params": {}}],
        "workflow_runs.html": [{"runs": [run], "progress": {7: {"done": 1, "total": 3}}, "project": "DEMO",
                                "status": "paused", "statuses": tuple(ui.RUN_STATUS_LABELS), "projects": ["DEMO"]},
                               {"runs": [], "progress": {}, "project": "", "status": "", "statuses": (),
                                "projects": []}],
        "workflow_run.html": [{"run": run, "info": run_info, "chain": chain, "attempts": [attempt],
                               "can_control": True, "names": {1: "alex"}, "steps": ["write"], "base": "/w"},
                              {"run": SimpleNamespace(**{**vars(run), "status": "running", "finished_at": None}),
                               "info": {**run_info, "params": "***"}, "chain": chain,
                               "attempts": [{**attempt, "outputs": "***", "error_code": None}],
                               "can_control": True, "names": {}, "steps": [], "base": "/w"},
                              {"run": run, "info": {**run_info, "params": {}}, "chain": [], "attempts": [],
                               "can_control": False, "names": {}, "steps": [], "base": "/w"}],
        "_workflow_chain.html": [{"chain": chain, "chain_task": "DEMO-0001"}, {"chain": []}],
        "_workflow_cell.html": [{"c": cell}, {"c": {**cell, "task_nr": None, "repaired": False}}, {"c": None}],
        "_workflow_badge.html": [{"t": {**card, "workflow": {"run_id": 7, "seq": 7, "key": "monthly",
                                                            "name": "Monthly"}}}],
        "_workflow_filter.html": [{"workflow_filter": f, "workflow_options": [{"id": 1, "label": "DEMO/monthly"}]}
                                  for f in ("hide", "all", "only", "1")],
        "_workflow_hidden_hint.html": [{"workflow_filter": "hide", "workflow_hidden": n, "workflow_show_url": "/k"}
                                       for n in (1, 3)],
        "_workflow_tabs.html": [{}, {"request": _request("/workflow-runs/7")}],
    }


def _settings_groups(setting: dict, variant: int) -> list[dict]:
    """Every setting of Admin → Settings as `admin_settings.group_view` passes it; variant 1 has nothing stored."""
    from app.services import settings_store
    from app.web import admin_settings

    view = {key: {**setting, "label": d.label, "kind": d.kind, "secret": d.secret, "choices": list(d.choices),
                  "value": d.choices[0] if d.choices else ("true" if d.kind == "bool" else "1")}
            for key, d in settings_store.DEFS.items()}
    if variant:
        view = {key: {**v, "value": "false" if v["kind"] == "bool" else v["value"], "source": "env",
                      "is_set": key != "smtp_password"} for key, v in view.items()}
    return admin_settings.group_view(admin_settings.browser_view(view))


def _contexts(name: str) -> list[dict]:
    """Provide contexts per template to render as many text-bearing branches as possible."""
    user = SimpleNamespace(name="alex", is_admin=True)
    base = {"request": _request(), "user": user, "db_ready": True, "csrf": "tok",
            "qc_projects": [("DEMO", "Demo project")], "error": "Something failed", "message": "All fine",
            "message_kind": "ok", "kind": "ok", "section": "users", "nr": "DEMO-0001"}
    card = {"task_nr": "DEMO-0001", "title": "Card", "prio": 1, "status": "in_progress", "project": "DEMO",
            "assigned_to": "builder", "created_at": NOW, "updated_at": NOW, "pr_url": "https://git.example/pr/1",
            "last_question": "Which **one**?", "result_summary": "Done", "sprint": 7, "model_tier": "simple",
            "model": None, "data_class": None, "data_class_effective": "local_only", "task_kind": "work"}
    owner_ctx = {"owner": "me", "owners": ["alex"], "owner_hint": True}
    locked_ctx = {**owner_ctx, "owner_locked": True}  # Profile switch "Show all projects" off.
    inbox_extra = {"n_reviews": 1, "n_questions": 1, "tab": "all", "notices": [
        {"event": "conflict", "label": "Merge, brain or changelog conflict", "key": "DEMO-0001",
         "actor": "codex", "created_at": NOW, "read": False},
        {"event": "sprint_done", "label": "Sprint completed", "key": None, "actor": None,
         "created_at": NOW, "read": True}], "notices_all": False}
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
    late = [{"task_nr": "DEMO-0009", "title": "Base", "status": "draft", "sprint": 8, "sprint_name": "Sprint B",
             "sprint_status": "planned"},
            {"task_nr": "DEMO-0010", "title": "Loose", "status": "defined", "sprint": None, "sprint_name": None,
             "sprint_status": None}]
    sprint_task = {**card, "status": "draft", "missing": ["description", "acceptance_criteria"], "waits_for": late}
    preview = {"released": [{"task_nr": "DEMO-0003", "title": "Ready"}],
               "blocked": [{"task_nr": "DEMO-0001", "title": "Card", "missing": ["description"]}],
               "waiting": [{"task_nr": "DEMO-0003", "title": "Ready", "waits_for": late}]}
    sprint_page = {"tasks": [sprint_task], "targets": [SimpleNamespace(id=8, name="Sprint B", status="planned")],
                   "agents": ["builder"], "can_plan": True, "can_manage": True, "total": 3, "done": 1,
                   "cancelled": 0, "open_count": 2, "batch_format": "Title | Agent | Prio", "restorable": 0}
    project = SimpleNamespace(id=1, project_key="DEMO", name="Demo", description="A demo", owner_id=1,
                              status="idea", privacy="local_only", gitea_url="https://git.example/r",
                              gitea_owner="team", gitea_repo="demo", default_branch="main", brain_path="brain.md",
                              data_four_eyes=None)
    owner = SimpleNamespace(name="alex", is_active=False)
    # Filter choices of the projects overview (`app/web/projects_ui.py`).
    PROJECT_STATUS_CHOICES = (("active", ("active",)), ("paused", ("paused",)), ("archived", ("archived",)),
                              ("all", PROJECT_STATUSES))
    PROJECT_SORT_CHOICES = ("activity", "key", "open")
    actor = SimpleNamespace(
        name="builder", display_name="Builder", is_active=False, actor_type="human", agent_kind="other",
        role="worker", runtime="local", model_location="cloud", lease_minutes=60, max_parallel_tasks=1, email="b@example.org",
        tailscale_login="b@github", notes="", last_seen_at=NOW, api_key_hash="x", api_key_prefix="tm_abc",
        api_key_created_at=NOW, gitea_username="builder", gitea_token_last4="9876", gitea_email=None,
        gitea_token_checked_at=NOW, id=1)
    member = SimpleNamespace(gitea_permission="write", sync_error="boom", gitea_synced_at=NOW)
    log_row = SimpleNamespace(created_at=NOW, event="review", task_nr="DEMO-0001", status="sent", attempts=1,
                              last_error="Telegram 403: blocked", channel="email")
    profile = {"actor": SimpleNamespace(**{**vars(actor), "role": "admin"}), "bot_enabled": True,
               "bot_username": None, "link": None, "pending": None, "events": {"review": "Review"},
               "enabled": {"review"}, "quiet_start": "22:00", "quiet_end": "07:00", "all_projects": True,
               "mute_all": None, "mute_review": None, "forever": FOREVER, "log": [log_row],
               "status_labels": {"sent": "sent"}, "status_tone": {"sent": "ok", "failed": "warn"},
               "mute_options": [("keep", "unchanged")],
               "channels": {"telegram": "Telegram", "email": "Email"}, "chosen": {"review": {"email"}},
               "bundle": 15, "bundle_choices": (0, 15, 60), "mail_enabled": True, "email_verified": False,
               "email_pending": None}
    mail_actor = SimpleNamespace(**{**vars(profile["actor"]), "email": "alex@example.org", "email_verified_at": NOW})
    probe = SimpleNamespace(ok=True, server_version="10.5", skip_locked=False, current_revision=None,
                            head_revision="0009", schema_current=False, error="refused")
    setting = {"value": "x", "is_set": True, "source": "db", "choices": []}
    plan_card = {"task_nr": "DEMO-0002", "title": "Card", "status": "draft", "prio": 2, "assigned_to": "builder",
                 "waits": ["DEMO-0001"]}
    plan_sprints = [SimpleNamespace(id=7 + i, name=f"Wave {i}", status=st, goal="Ship it" if i else None,
                                    start_date=date(2026, 10, 1) if i else None, end_date=None)
                    for i, st in enumerate(("released", "planned", "closed"))]
    plan_rows = [{"sprint": x, "cards": [plan_card] if x.status != "planned" else [],
                  "total": 2 if x.status != "planned" else 0, "done": 1, "open": 1, "drafts": 1,
                  "empty": x.status == "planned", "moved_out": 2 if x.status == "closed" else 0}
                 for x in plan_sprints]
    planning = {"projects": [("DEMO", "Demo project")], "project": {"key": "DEMO", "name": "Demo project"},
                "show_closed": False, "active": "all", "owner_hint": False, "sprint_count": len(plan_rows),
                "counts": {"active": 1, "planned": 1, "closed": 1, "all": 3},
                "targets": plan_sprints[:2], "rows": plan_rows}
    ctxs = {
        "base.html": [{}, {"user": None, "db_ready": False}],
        "_app_header.html": [{}, {"user": None, "db_ready": False}],
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
                                     (owner_ctx, {"done_range": "today", "done_total": 1, "done_all": 5,
                                                  "workflow_filter": "hide", "workflow_hidden": 2,
                                                  "workflow_show_url": "/kanban?workflow=all",
                                                  "workflow_options": [{"id": 1, "label": "DEMO/monthly"}]}))],
        "_card.html": [{"t": {**card, "status": s}} for s in ("defined", "questions", "aborted")]
        + [{"t": {**card, "data_class": "sealed", "data_class_effective": "sealed", "task_kind": "run"}}],
        "_data_class.html": [{"cls": c, "compact": compact, "inherited": compact}
                             for c in ("cloud_ok", "local_only", "sealed") for compact in (True, False)],
        "_data_class_field.html": [{"dc_value": "inherit", "dc_project": None},
                                   {"dc_value": "sealed", "dc_project": "local_only", "dc_disabled": ["cloud_ok"]}],
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
             "tasks": [], "total": 0, "restorable": 2})],
        "_release_preview.html": [{"p": project, "sprint": sprint, "preview": preview, "back": "planning"},
                                  {"p": project, "sprint": {**sprint, "status": "released"},
                                   "preview": {"released": [], "blocked": []}}],
        "_late_deps.html": [{"t": {"waits_for": late}}, {"t": {"waits_for": []}}],
        "_plan_row.html": [{"t": plan_card, "sprint": plan_sprints[0], "targets": plan_sprints[:2],
                            "no_sprint": "none"},
                           {"t": {**plan_card, "assigned_to": None, "waits": []}, "sprint": {"id": None},
                            "targets": plan_sprints[:2], "no_sprint": "none"}],
        "_plan_sprint.html": [{"r": plan_rows[0], "project": {"key": "DEMO"}, "can_manage": True,
                               "targets": plan_sprints[:2]},
                              {"r": {**plan_rows[0], "sprint": {**vars(plan_sprints[0]), "goal": None}},
                               "project": {"key": "DEMO"}, "can_manage": False, "targets": plan_sprints[:2]},
                              {"r": {**plan_rows[0], "empty": True, "cards": []}, "project": {"key": "DEMO"},
                               "can_manage": False, "targets": plan_sprints[:2]}],
        "planning.html": [
            {**planning, "can_manage": True},
            {**planning, "can_manage": False, "show_closed": True, "active": "planned"},
            {**planning, "project": None, "rows": [], "projects": [], "owner_hint": True, "sprint_count": 0},
            {**planning, "project": None, "rows": []}],
        "sprint_editor.html": [
            {**editor, "sprint": None, "key": None, "items": [], "entries_json": "[]", "error": "Boom",
             "form": {"project": "", "name": "", "goal": "", "start_date": "", "end_date": ""}},
            {**editor, "sprint": {**sprint, "project": "DEMO"}, "key": "DEMO", "manage_keys": [],
             "form": {"name": "Sprint A", "goal": "", "start_date": "", "end_date": ""}},
            {**editor, "sprint": None, "key": None, "items": [], "projects": [], "owner_hint": True,
             "form": {"project": "", "name": "", "goal": "", "start_date": "", "end_date": ""}}],
        "_owner_hint.html": [owner_ctx, locked_ctx],
        "_md_field.html": [{"md_label": "Label", "md_name": "description", "md_value": "x"}],
        "inbox.html": [{**owner_ctx, "managed": ["DEMO"], "mail_approvals": [{**MAIL, "task_title": "Login"}],
                        "data_releases": [{"id": 12, "task_nr": "DEMO-0003", "title": "Run", "dataset": "DS-0003",
                                           "warnings": 2, "blocked": True},
                                          {"id": 13, "task_nr": "DEMO-0004", "title": "Run", "dataset": None,
                                           "warnings": 0, "blocked": False}],
                        "inbox": {k: [{**card, "reviewer": "checker", "review_claimed": True}]
                                  for k in ("questions", "unassigned", "aborted", "for_review", "lease_expired",
                                            "agent_review")},
                        **inbox_extra},
                       {**locked_ctx, "managed": [],
                        "inbox": {k: [] for k in ("questions", "unassigned", "aborted", "for_review",
                                                  "lease_expired", "agent_review")}, **inbox_extra},
                       {**owner_ctx, "managed": ["DEMO"],
                        "inbox": {**{k: [] for k in ("questions", "unassigned", "aborted", "for_review",
                                                     "lease_expired", "agent_review")},
                                  **{k: [{**card, "reviewer": "alex", "task_kind": kind,
                                          "workflow": {"run_id": 7, "seq": 7, "key": "monthly", "name": "M"}}]
                                     for k, kind in (("workflow_questions", "work"), ("workflow_review", "approval"),
                                                     ("workflow_aborted", "work"))}},
                        **inbox_extra}],
        **_data_contexts(project),
        **_workflow_contexts(project, card),
    "projects.html": [{"items": [project], "current": None, "current_ids": set(), "numbers": {1: {"counts": {"defined": 2, "for_review": 1, "in_progress": 1}, "last_activity": NOW, "agents": 1}}, "owners": {1: owner},
                           "can_create": True, "mixed": {project.id}, "status_choices": PROJECT_STATUS_CHOICES,
                           "sort_choices": PROJECT_SORT_CHOICES, "open_count": lambda c: sum(c.values()),
                           "q": "", "status": "active", "sort": "activity", "view": "list", "mine": "mine",
                           "active_total": 3},
                          {"items": [], "current": None, "current_ids": set(), "numbers": {}, "owners": {}, "can_create": False,
                           "status_choices": PROJECT_STATUS_CHOICES, "sort_choices": PROJECT_SORT_CHOICES,
                           "open_count": lambda c: sum(c.values()), "q": "", "status": "all", "sort": "key",
                           "view": "list", "mine": "all", "own_only": True, "active_total": 0},
                          {"items": [project], "current": project, "current_ids": {project.id},
                           "numbers": {1: {"counts": {}, "last_activity": None, "agents": 0}}, "owners": {1: owner},
                           "can_create": True, "mixed": set(), "status_choices": PROJECT_STATUS_CHOICES,
                           "sort_choices": PROJECT_SORT_CHOICES, "open_count": lambda c: sum(c.values()),
                           "q": "x", "status": "archived", "sort": "open", "view": "cards", "mine": "all",
                           "active_total": 1},
                          {"items": [], "current": None, "current_ids": set(), "numbers": {}, "owners": {}, "can_create": True,
                           "status_choices": PROJECT_STATUS_CHOICES, "sort_choices": PROJECT_SORT_CHOICES,
                           "open_count": lambda c: sum(c.values()), "q": "", "status": "paused", "sort": "activity",
                           "view": "cards", "mine": "mine", "own_only": True, "active_total": 1}],
        "project_new.html": [{"humans": ["alex"], "form": {"gitea_mode": m, "status": "active",
                                                           "privacy": "local_only", "owner": "alex"}}
                             for m in ("create", "link", "none")],
        "project_detail.html": [{"p": project, "info": {"clone_url": "ssh://x", "default_reviewer": "gone"},
                                 "reviewers": filters["reviewers"], "counts": {"defined": 2},
                                 "members": [(member, actor)], "missing": ["brain.md"], "gitea_error": "down",
                                 "can_manage": True, "humans": ["alex"], "owner": owner, "can_plan": True, "mixed": True,
                                 "sprints": [SimpleNamespace(id=7, name="Sprint A", status="released",
                                                             start_date=None, end_date="2026-10-23")],
                                 "sprint_counts": {7: {"draft": 1}}, "mail_modes": MAIL_MODES, "mail_limit": "20",
                                 "four_eyes_default": True,
                                 "mail_recipients": [SimpleNamespace(id=1, address="client@example.org",
                                                                     label="Customer", mode="freigabe")]},
                                {"p": SimpleNamespace(**{**vars(project), "gitea_repo": None}), "info": {},
                                 "counts": {}, "members": [], "missing": [], "gitea_error": None,
                                 "can_manage": True, "humans": [], "owner": None, "mail_modes": MAIL_MODES},
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
        "_ui.html": [{}],  # Macros only; admin/ui_kit.html calls every one of them.
        "admin/ui_kit.html": [{"section": "ui_kit"}],
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
        "admin/settings.html": [{"groups": _settings_groups(setting, variant), "workflow_tick_status": tick,
            "channels": {"email": "Email"}, "admin_email": "admin@example.org",
            "notification_log": [(log_row, owner), (log_row, None)], "events": {"review": "Review"},
            "status_labels": {"sent": "sent"},
            "zone_findings": [{"actor": SimpleNamespace(**{**vars(actor), "actor_type": "agent"}),
                               "projects": ["DEMO"], "tasks": ["DEMO-0001"], "reviews": []}],
            "zone_enforced": True} for variant, tick in ((0, {"enabled": True, "stale": True, "last": "2026-10-17 12:00"}),
                                                         (1, {"enabled": False, "stale": False, "last": None}))],
        "admin/_model_location.html": [{"current": "cloud"}, {"current": "none"}],
        "profile.html": [{**profile, "link": SimpleNamespace(chat_name="alex", chat_id=1, linked_at=NOW)},
                         {**profile, "bot_enabled": False},
                         {**profile, "code": "abc", "code_expires": NOW, "bot_username": "tb_bot",
                          "pending": SimpleNamespace(expires_at=NOW)},
                         {**profile, "pending": SimpleNamespace(expires_at=NOW),
                          "mute_all": NOW.replace(tzinfo=None), "mute_review": FOREVER},
                         {**profile, "actor": mail_actor, "email_verified": True, "mail_enabled": False},
                         {**profile, "actor": mail_actor, "email_pending": SimpleNamespace(expires_at=NOW)},
                         {**profile, "actor": SimpleNamespace(**{**vars(mail_actor), "email_verified_at": None})}],
        "email_verified.html": [{}],
        "admin/users.html": [{"actors": [actor, SimpleNamespace(**{**vars(actor), "actor_type": "agent",
                                                                   "is_active": True, "api_key_hash": None,
                                                                   "gitea_username": None})],
                              "counts": {1: {"current": "DEMO-0001", "open": 2}}, "owners": {1: "alex"},
                              "filters": f, "filtered": any(f.values()), "total": 3, "owner_choices": ["alex"],
                              "roles": ["worker"], "kinds": ["other"],
                              "zone_max": {actor.id: "local_only"}}
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
        chain = [{"key": "write", "type": "agent", "position": 1, "attempt": 1, "status": "active", "symbol": "●",
                  "task_nr": "DEMO-0001", "repaired": False, "error_code": None, "duration": None,
                  "data_class": "cloud_ok", "current": True}]
        variants.append({**task_ctx, "t": _task("in_progress", True, True), "can_manage": False,
                         "can_review": False, "human_admin": False,
                         "workflow": {"run_id": 7, "seq": 7, "name": "Monthly", "key": "monthly", "project": "DEMO",
                                      "position": 1, "total": 3, "step_key": "write", "status": "running",
                                      "chain": chain}})
        variants.append({**task_ctx, "t": {**_task("draft", True, False), "sprint": 7, "sprint_name": "Sprint A",
                                           "sprint_status": "planned"},
                         "project_sprints": filters["sprints"], "can_manage": True, "can_review": False,
                         "human_admin": False})
        # Runner task with output gate results: release-view box instead of "Approve & merge".
        variants.append({**task_ctx, "t": {**_task("for_review", True, True), "task_kind": "run"},
                         "can_manage": True, "can_review": True, "human_admin": False,
                         "gate_results": [{"id": 12, "status": "pending_approval", "label": "Awaits release"},
                                          {"id": 11, "status": "rejected", "label": "Rejected"}]})
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
        cleared = {**MAIL, "id": 2, "status": "sent", "body": None, "cleared": True, "sent_at": NOW,
                   "message_id": "<x@example.org>", "smtp_error": "550 refused", "approved_by": "alex",
                   "approved_at": NOW, "decided_note": "ok", "reply_to": "alex@example.org"}
        for decide in (True, False):
            variants.append({**task_ctx, "t": review, "emails": [MAIL, cleared], "can_decide_mail": decide,
                             "can_manage": True, "can_review": True, "can_answer": True, "human_admin": True})
        for applies in (True, False):
            variants.append({**task_ctx, "t": {**_task("defined", True, True), "model_tier": "complex", "model": None,
                                               "model_effective": {"model": "opus", "source": "tier",
                                                                   "applies": applies}},
                             "can_manage": True, "can_review": False, "human_admin": False})
        return [{**base, **v} for v in variants]
    if name in ("workflow_trigger_edit.html", "workflow_triggers.html"):
        from app.models import WorkflowTrigger
        from app.services.workflows.schedules import OUTCOME_LABELS
        template = SimpleNamespace(name="Reports", project=SimpleNamespace(project_key="DEMO"), workflow_key="reports")
        row = WorkflowTrigger(id=1, kind="dataset_ready", filter_json="{}", params_json="{}", enabled=True)
        values = {"template": template, "templates": [template], "row": row, "triggers": [row],
                  "entries": [(template, row)], "kinds": ("dataset_ready", "workflow_succeeded", "workflow_failed"),
                  "fires": [SimpleNamespace(event_id=1, outcome=key, run_id=1) for key in OUTCOME_LABELS],
                  "outcome_labels": OUTCOME_LABELS, "can_edit": True, "error": "",
                  "filter_text": "{}", "params_text": "{}"}
        return [{**base, **values}, {**base, **values, "can_edit": False, "filter_text": "***", "params_text": "***"}]
    if name in ("workflow_schedule_edit.html", "workflow_schedules.html"):
        from app.models import WorkflowSchedule
        from app.services.workflows.schedules import OUTCOME_LABELS
        template = SimpleNamespace(name="Reports", project=SimpleNamespace(project_key="DEMO"), workflow_key="reports")
        row = WorkflowSchedule(id=1, name="Monthly", kind="workday", cron_expr="0 7 * * mon-fri",
            workday_rule_json='{"nth":3,"of":"month","time":"07:00"}', timezone="Europe/Berlin",
            params_json="{}", holiday_shift="skip", overlap_policy="queue", misfire_policy="run_late",
            misfire_grace_min=360, enabled=True)
        values = {"template": template, "templates": [template], "row": row, "schedules": [row],
                  "entries": [(template, row, ["2027-01-07T07:00+01:00"])], "enabled": False,
                  "stale_tick": True, "last_tick": None, "error": "", "can_edit": True,
                  "params_text": "{}", "rule": {"nth":3,"of":"month","time":"07:00"}, "next_fires": ["2027-01-07T07:00+01:00"], "outcome_labels": OUTCOME_LABELS,
                  "calendars": [SimpleNamespace(id=2, name="DE-BW", country="DE", subdivision="BW")],
                  "fires": [SimpleNamespace(scheduled_for=NOW, outcome=key, run_id=1) for key in OUTCOME_LABELS]}
        return [{**base, **values}, {**base, **values, "can_edit": False, "params_text": "***"}]
    if name in ("admin/users.html", "admin/_user_panel.html"):
        from starlette.datastructures import URL
        from app.web.admin_users import presence

        variants = []
        for old in ctxs["admin/users.html"]:
            rows = old["actors"]
            people = [a for a in rows if a.actor_type == "human"]
            agents = [a for a in rows if a.actor_type == "agent"]
            for selected in rows + [None]:
                if name == "admin/_user_panel.html" and selected is None:
                    continue
                variants.append({**base, **old, "actor": selected, "humans": ["alex"],
                    "filters": {**old["filters"], "q": "", "type": ""},
                    "list_url": URL("http://testserver/admin/users"),
                    "type_counts": {"agent": len(agents), "human": len(people)},
                    "presence": {a.id: presence(a, NOW) for a in rows},
                    "groups": [{"agents": True, "owner": "alex", "rows": agents, "online": 0},
                               {"agents": False, "rows": people}]})
        return variants
    if name == "_backlog_content.html":
        return _contexts("backlog.html")
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
    client.get("/language", params={"lang": "de", "next": "/login"})
    assert client.cookies.get(i18n.LANG_COOKIE) == "de"
    r = client.get("/language", params={"lang": "default", "next": "/login"}, follow_redirects=False)
    assert "Max-Age=0" in r.headers["set-cookie"]
    assert client.cookies.get(i18n.LANG_COOKIE) is None
    assert ">Log in</h2>" in client.get(r.headers["location"]).text


def test_invalid_cookie_falls_back_to_default(client, english_default):
    client.cookies.set(i18n.LANG_COOKIE, "xx")
    assert ">Log in</h2>" in client.get("/login").text


@pytest.mark.parametrize("cookie,active", [(None, "default"), ("en", "en"), ("de", "de"), ("xx", "default")])
@pytest.mark.parametrize("name", ["_app_header.html", "profile.html", "admin/settings.html"])
def test_language_choices_and_override_hints(monkeypatch, name, cookie, active):
    monkeypatch.setattr(get_settings(), "ui_language", "de")
    request = _request("/profile", b"tab=display&x=1")
    if cookie:
        request.scope["headers"] = [(b"cookie", f"tm_lang={cookie}".encode())]
    ctx = {**_contexts(name)[0], "request": request}
    with use_language("en"):
        html = templates.get_template(name).render(ctx)
    assert "Default (DE)" in html
    # Header is shared at every viewport; profile also has its own language controls.
    choices = re.findall(r'<a[^>]+href="/language\?lang=(default|en|de)&amp;next=[^"]*"[^>]*>', html)
    assert set(choices) == {"default", "en", "de"}
    active_choices = re.findall(r'<a[^>]+href="/language\?lang=(default|en|de)&amp;next=[^"]*"[^>]*aria-current="true"', html)
    assert active_choices and set(active_choices) == {active}
    if name == "profile.html":
        assert ("Personal choice:" if cookie in ("en", "de") else "Using the default language: DE") in html
    if name == "admin/settings.html":
        assert "Applies to everyone without a personal language choice." in html
        assert ("Your own choice:" in html) == (cookie in ("en", "de"))
        if cookie in ("en", "de"):
            assert f"Your own choice: {cookie.upper()}" in html
            assert 'href="/language?lang=default&amp;next=/admin/settings"' in html


def test_setting_update_changes_next_request_without_cookie(client, monkeypatch):
    from app.models import AppSetting
    from app.services import settings_store

    rows = {}
    session = SimpleNamespace(get=lambda model, key: rows.get(key),
                              add=lambda row: rows.update({row.setting_key: row}),
                              delete=lambda row: rows.pop(row.setting_key), flush=lambda: None)
    monkeypatch.setattr(i18n, "_load_default", lambda: rows.get("ui_language", AppSetting(setting_value="en")).setting_value)
    i18n.clear_default_cache()
    assert '<html lang="en" ' in client.get("/login").text  # Populate the cache.
    settings_store.update(session, {"ui_language": "de"}, None)
    assert '<html lang="de" ' in client.get("/login").text
    client.get("/language", params={"lang": "en", "next": "/login"})
    assert '<html lang="en" ' in client.get("/login").text
    response = client.get("/language", params={"lang": "default", "next": "/login"})
    assert '<html lang="de" ' in response.text
    settings_store.update(session, {"ui_language": None}, None)
    assert '<html lang="en" ' in client.get("/login").text


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
    client.get("/language", params={"lang": "de", "next": "/login"})
    assert ">Anmelden</h2>" in client.get("/login").text  # Personal choice overrides the setting.
    client.get("/language", params={"lang": "default", "next": "/login"})
    assert client.cookies.get(i18n.LANG_COOKIE) is None
    assert ">Log in</h2>" in client.get("/login").text
    with ready_db.session() as s:
        settings_store.update(s, {"ui_language": "de"}, None)
        s.commit()
    assert ">Anmelden</h2>" in client.get("/login").text
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
