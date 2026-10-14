"""Notification settings over the API: `tbr notify`, MCP `notify` and `/api/v1/notifications/settings` – own
settings, orchestrators for linked humans (audit + Telegram notice), workers not. The Bot API is faked."""
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import db
from app.models import Actor
from app.services import notifications
from app.services.actors import utcnow
from cli import client as client_mod
from cli.mcp_server import TaskBrigadeMCP
from tests.conftest import bearer, make_token
from tests.test_telegram import deliver, fake, link, rows, tenv  # noqa: F401 (Fixtures)
from tests.test_ui_cli import tm_as  # noqa: F401 (Fixture)
from tests.test_workflow import env, gitea  # noqa: F401 (Fixtures)

ROOT = Path(__file__).resolve().parents[1]
URL = "/api/v1/notifications/settings"


def pref(name):
    with db.session() as s:
        return notifications.prefs(s, s.scalar(select(Actor.id).where(Actor.name == name)))


def audits(caplog, action):
    return [r for r in caplog.records if getattr(r, "audit_action", None) == action]


@pytest.fixture
def nenv(tenv):  # noqa: F811
    """tenv plus openclaw-agent as orchestrator; alex (member) linked with Telegram chat 555."""
    tenv["orch"] = bearer(make_token("openclaw-agent", role="orchestrator"))
    link("alex", 555)
    return tenv


@pytest.fixture
def mcp_as(nenv, tm_as):  # noqa: F811
    def make(who):
        token = nenv[who]["Authorization"].split()[1]
        return TaskBrigadeMCP(client_mod.Client(client_mod.Config("http://testserver", token)), None)
    return make


# --- Without a database ---------------------------------------------------------------------------

def test_settings_notice_is_never_muted():
    now = utcnow()
    p = notifications.NotificationPref(mute_all_until=notifications.FOREVER, mute_review_until=notifications.FOREVER)
    assert notifications.is_muted(p, "question", now)
    assert not notifications.is_muted(p, notifications.SETTINGS_EVENT, now)


def test_notify_report_formats_states():
    data = {"user": "alex", "timezone": "Europe/Berlin", "telegram_linked": True, "bot_enabled": True,
            "all": {"muted": True, "until": "2026-10-10T13:00:00", "until_local": "2026-10-10 15:00",
                    "forever": False},
            "reviews": {"muted": True, "forever": True}, "quiet_hours": "22:00–07:00", "changed": "pause"}
    out = client_mod.notify_report(data)
    assert out.splitlines()[0] == "All messages muted."
    assert "All messages: muted until 2026-10-10 15:00 (Europe/Berlin)" in out
    assert "Review requests: muted until switched back on" in out and "Quiet hours: 22:00–07:00" in out
    out = client_mod.notify_report({**data, "telegram_linked": False, "changed": None,
                                    "all": {"muted": False}, "reviews": {"muted": False}})
    assert "All messages: on" in out and "Not linked with Telegram" in out


# --- With the test database -----------------------------------------------------------------------

@pytest.mark.db
def test_own_settings_via_cli(nenv, tm_as, caplog):  # noqa: F811
    r = tm_as(nenv["alex"], "notify", "status")
    assert r.exit_code == 0, r.output
    assert "Notifications of alex:" in r.stdout and "All messages: on" in r.stdout
    caplog.set_level(logging.INFO, logger="audit")
    r = tm_as(nenv["alex"], "notify", "pause", "2h")
    assert r.exit_code == 0 and "All messages muted." in r.stdout and "muted until" in r.stdout, r.output
    until = pref("alex").mute_all_until
    assert timedelta(hours=1, minutes=59) < until - utcnow() <= timedelta(hours=2)
    a = audits(caplog, "notifications_muted")
    assert a and a[-1].audit_actor == "alex" and a[-1].target == "alex" and a[-1].via == "api"
    r = tm_as(nenv["alex"], "notify", "reviews", "off")
    assert r.exit_code == 0 and "Review requests: muted until switched back on" in r.stdout, r.output
    assert pref("alex").mute_review_until == notifications.FOREVER
    assert tm_as(nenv["alex"], "notify", "resume").exit_code == 0
    assert tm_as(nenv["alex"], "notify", "reviews", "on").exit_code == 0
    p = pref("alex")
    assert p.mute_all_until is None and p.mute_review_until is None
    r = tm_as(nenv["alex"], "notify", "pause", "until", "18:00", "--json")
    assert r.exit_code == 0 and '"changed": "pause"' in r.stdout, r.output
    r = tm_as(nenv["alex"], "notify", "pause", "bald")
    assert r.exit_code == 1 and "invalid_duration" in r.output
    assert tm_as(nenv["alex"], "notify", "reviews", "vielleicht").exit_code == 2
    # No notice to yourself.
    assert rows(event=notifications.SETTINGS_EVENT) == []


@pytest.mark.db
def test_orchestrator_mutes_linked_human_with_audit_and_notice(nenv, tm_as, caplog):  # noqa: F811
    caplog.set_level(logging.INFO, logger="audit")
    with db.session() as s:  # alex had muted everything: the notice still arrives
        notifications.save_prefs(s, s.scalar(select(Actor).where(Actor.name == "alex")),
                                 mute_all=notifications.FOREVER)
        s.commit()
    r = tm_as(nenv["orch"], "notify", "reviews", "off", "1d", "--user", "alex")
    assert r.exit_code == 0 and "Review requests muted." in r.stdout and "Notifications of alex:" in r.stdout, \
        r.output
    assert pref("alex").mute_review_until > utcnow() + timedelta(hours=23)
    a = audits(caplog, "notifications_muted")
    assert a and a[-1].audit_actor == "openclaw-agent" and a[-1].target == "alex" and a[-1].scope == "reviews"
    notice = rows(event=notifications.SETTINGS_EVENT)
    assert len(notice) == 1 and notice[0].status == "queued"
    assert deliver()["sent"] == 1
    chat, text = nenv["fake"].sent[-1]
    assert chat == 555 and "openclaw-agent hat deine TaskBrigade-Benachrichtigungen geändert" in text
    assert "Review-Aufforderungen stummgeschaltet bis" in text
    r = tm_as(nenv["orch"], "notify", "status", "--user", "alex")
    assert r.exit_code == 0 and "Review requests: muted until" in r.stdout
    r = tm_as(nenv["orch"], "notify", "resume", "-u", "alex")
    assert r.exit_code == 0 and pref("alex").mute_all_until is None
    assert audits(caplog, "notifications_unmuted")[-1].target == "alex"
    # The log shows the event with a label.
    assert len(rows(event=notifications.SETTINGS_EVENT)) == 2


@pytest.mark.db
def test_rights_worker_and_unlinked_humans(nenv, tm_as):  # noqa: F811
    c = nenv["c"]
    # A worker agent may not change other people's settings – and has none of its own.
    r = tm_as(nenv["claude"], "notify", "pause", "--user", "alex")
    assert r.exit_code == 1 and "403" in r.output
    assert pref("alex").mute_all_until is None
    r = tm_as(nenv["claude"], "notify", "status")
    assert r.exit_code == 1 and "notify_user_required" in r.output
    # A member human may not change someone else's settings either.
    r = c.post(URL, headers=nenv["alex"], json={"action": "pause", "user": "admin"})
    assert r.status_code == 403
    # Orchestrators: only humans linked with Telegram (admin is not linked, openclaw-agent is an agent).
    r = c.post(URL, headers=nenv["orch"], json={"action": "pause", "user": "admin"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_linked"
    r = c.post(URL, headers=nenv["orch"], json={"action": "pause", "user": "codex"})
    assert r.status_code == 404
    r = c.get(URL, headers=nenv["orch"], params={"user": "niemand"})
    assert r.status_code == 404
    r = c.post(URL, headers=nenv["orch"], json={"action": "explode", "user": "alex"})
    assert r.status_code == 422
    # Admins may do the same as orchestrators.
    r = c.post(URL, headers=nenv["admin"], json={"action": "pause", "until": "30m", "user": "alex"})
    assert r.status_code == 200 and r.json()["all"]["muted"] and not r.json()["all"]["forever"]


@pytest.mark.db
def test_notify_via_mcp(nenv, mcp_as):  # noqa: F811
    orch, worker = mcp_as("orch"), mcp_as("claude")
    out = orch.notify("pause", "2h", user="alex")
    assert "All messages muted." in out and "Notifications of alex:" in out
    assert pref("alex").mute_all_until is not None
    assert "Review requests: on" in orch.notify("status", user="alex")
    assert "Unknown action" in orch.notify("explode", user="alex")
    assert "403" in worker.notify("resume", user="alex")
    assert pref("alex").mute_all_until is not None
    assert "Messages unmuted." in orch.notify("resume", user="alex")
    assert len(rows(event=notifications.SETTINGS_EVENT)) == 2


@pytest.mark.db
def test_projects_and_agents_commands(nenv, tm_as):  # noqa: F811
    r = tm_as(nenv["orch"], "projects")
    assert r.exit_code == 0, r.output
    lines = r.stdout.splitlines()
    assert any(line.startswith("KET ") and "cloud_ok" in line for line in lines)
    assert any(line.startswith("OWN ") and "local_only" in line and "alex" in line for line in lines)
    r = tm_as(nenv["orch"], "agents")
    assert r.exit_code == 0 and "codex" in r.stdout and "cloud" in r.stdout, r.output
    assert any(line.startswith("alex ") and "human" in line for line in r.stdout.splitlines())
    assert '"name": "openclaw-agent"' in tm_as(nenv["orch"], "agents", "--json").stdout


# --- Skill and docs --------------------------------------------------------------------------------

SKILLS = [ROOT / "deploy/agents/openclaw/taskbrigade/SKILL.md",
          ROOT / "deploy/agents/openclaw/taskbrigade-telegram/SKILL.md"]


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_openclaw_skill_describes_orchestration_by_free_text(path):
    text = path.read_text(encoding="utf-8")
    section = text[text.index("## Orchestrator via Telegram"):]
    for rule in ("tbr create", "--draft", "tbr sprint create", "tbr sprint add-tasks", "tbr sprint release",
                 "tbr notify pause", "tbr notify resume", "tbr notify reviews off", "--user",
                 "tbr list", "Ask back", "Confirm before", "task number and link", "only `tbr` commands",
                 "never approve", "orchestrator"):
        assert rule in section, rule


def test_docs_describe_openclaw_orchestrator_setup():
    text = (ROOT / "docs/agent-frameworks.md").read_text(encoding="utf-8")
    for needle in ("role `orchestrator`", "tbr setup openclaw", "--openclaw-agent", "tokens", "tbr notify"):
        assert needle in text, needle
