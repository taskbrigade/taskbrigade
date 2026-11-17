"""Blocked agents (login expired, usage limit): server state, runner report/recovery, inbox, badges, reminders."""
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db import db
from app.models import Actor, Notification
from app.services import agent_blocks, notifications, settings_store
from cli import runner
from tests.test_ui_cli import post, ui  # noqa: F401
from tests.test_workflow import env, gitea, mkproject, mktask  # noqa: F401

BLOCK = {"kind": "login", "reason": "claude-code: login expired or not logged in – run `claude /login` on box, "
                                    "then tbr doctor", "host": "box", "task_nr": "KET-0001"}


# --- Runner (no DB) -------------------------------------------------------------------------------------------------

class FakeClient:
    def __init__(self, me=None, fail=False):
        self.calls, self.me, self.fail = [], me if me is not None else {"name": "claude-code"}, fail

    def request(self, method, path, **kw):
        if self.fail:
            raise runner.ApiError(404, "not found")
        self.calls.append((method, path, kw.get("json")))

    def get(self, path):
        self.calls.append(("GET", path, None))
        return self.me


def test_write_block_reports_to_server_and_logs(tmp_path):
    fc = FakeClient()
    runner.write_block(tmp_path, "login", BLOCK["reason"], "KET-0001", fc)
    block = runner.read_block(tmp_path)
    assert block["reported"] is True and block["host"]
    method, path, body = fc.calls[0]
    assert (method, path) == ("POST", "/agents/me/blocked")
    assert body["kind"] == "login" and body["task_nr"] == "KET-0001" and body["since"] == block["at"]
    log = (tmp_path / "logs" / "runner.log").read_text()
    assert "Agent blocked (login)" in log and "reported to the server" in log


def test_write_block_keeps_working_without_server(tmp_path):
    runner.write_block(tmp_path, "unsent", "prompt unsent", "KET-0001", FakeClient(fail=True))
    assert runner.read_block(tmp_path)["reported"] is False
    assert "next pass tries again" in (tmp_path / "logs" / "runner.log").read_text()
    # Kinds the server does not know are reported as "other".
    fc = FakeClient()
    assert runner.report_block(fc, {"kind": "weird", "reason": "x"}) and fc.calls[0][2]["kind"] == "other"


def _blocked(tmp_path, **extra):
    block = {**BLOCK, "at": datetime.now(UTC).isoformat(timespec="seconds"), "reported": True, **extra}
    (tmp_path / runner.BLOCK_FILE).write_text(json.dumps(block))
    return block


def test_recovery_login_check_clears_block(tmp_path, monkeypatch):
    block = _blocked(tmp_path)
    status = {"ok": False}
    monkeypatch.setattr("cli.doctor.login_status", lambda cfg: (status["ok"], ["claude", "auth", "status"]))
    fc = FakeClient(me={"name": "claude-code", "blocked": {"kind": "login"}})
    assert runner.recover_block(fc, {}, tmp_path, block) is None
    assert runner.read_block(tmp_path)
    status["ok"] = True
    line = runner.recover_block(fc, {}, tmp_path, block)
    assert line and "login check passed: claude auth status" in line
    assert runner.read_block(tmp_path) is None
    assert ("DELETE", "/agents/me/blocked", None) in fc.calls
    assert "Agent ready again (login check passed" in (tmp_path / "logs" / "runner.log").read_text()


def test_recovery_follows_admin_clear_on_server(tmp_path, monkeypatch):
    block = _blocked(tmp_path, kind="unsent")
    monkeypatch.setattr("cli.doctor.login_status", lambda cfg: pytest.fail("no login check needed"))
    line = runner.recover_block(FakeClient(me={"name": "claude-code", "blocked": None}), {}, tmp_path, block)
    assert "block cleared on the server" in line and runner.read_block(tmp_path) is None
    # Older servers send no `blocked` key: the block stays.
    block = _blocked(tmp_path, kind="unsent")
    assert runner.recover_block(FakeClient(me={"name": "claude-code"}), {}, tmp_path, block) is None


def test_recovery_limit_waits_for_reset_or_wait_time(tmp_path, monkeypatch):
    monkeypatch.setattr("cli.doctor.login_status", lambda cfg: (None, ["x"]))
    at = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    block = _blocked(tmp_path, kind="limit", reason="usage limit reached (5-hour limit)", at=at.isoformat())
    fc = FakeClient(me={"name": "claude-code", "blocked": {"kind": "limit"}})
    cfg = {"limit_wait_minutes": 30}
    assert runner.recover_block(fc, cfg, tmp_path, block, now=at + timedelta(minutes=29)) is None
    assert "usage limit wait over" in runner.recover_block(fc, cfg, tmp_path, block, now=at + timedelta(minutes=31))
    assert runner.read_block(tmp_path) is None


def test_limit_resume_at_reads_reset_time():
    at = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    local = at.astimezone()
    reset_hour = (local.hour + 3) % 24
    block = {"at": at.isoformat(), "reason": f"You've hit your limit · resets {reset_hour}:00"}
    resume = runner.limit_resume_at(block, 60).astimezone()
    assert (resume.hour, resume.minute) == (reset_hour, 1) and resume > local
    pm = runner.limit_resume_at({"at": at.isoformat(), "reason": "limit reached ∙ resets 3pm"}, 60).astimezone()
    assert pm.hour == 15
    assert runner.limit_resume_at({"at": at.isoformat(), "reason": "rate limit"}, 45) == at + timedelta(minutes=45)


def test_run_once_retries_report_while_blocked(tmp_path, monkeypatch):
    from tests.test_runner import write_config

    write_config(tmp_path, ["true"], mode="foreground")
    _blocked(tmp_path, reported=False)
    monkeypatch.setattr("cli.doctor.login_status", lambda cfg: (False, ["claude", "auth", "status"]))
    fc = FakeClient()
    msg = runner.run_once(fc, tmp_path)
    assert msg.startswith("Agent blocked (")
    assert [c for c in fc.calls if c[:2] == ("POST", "/agents/me/blocked")]
    assert runner.read_block(tmp_path)["reported"] is True


# --- Server -------------------------------------------------------------------------------------------------------



def _report(env, who="claude", **body):
    return env["c"].post("/api/v1/agents/me/blocked", headers=env[who], json={**BLOCK, **body})


def _link(owner_name="admin"):
    """Telegram on and the human linked, so notification rows are written."""
    from app.models import TelegramLink

    with db.session() as s:
        owner = s.scalar(select(Actor).where(Actor.name == owner_name))
        settings_store.update(s, {"telegram_enabled": "true", "telegram_bot_token": "123456:test-token"}, owner.id)
        s.add(TelegramLink(actor_id=owner.id, chat_id=12345))
        s.commit()


@pytest.mark.db
def test_report_and_clear_via_api(env):
    r = _report(env)
    assert r.status_code == 200, r.text
    assert r.json()["new"] is True and r.json()["blocked"]["kind"] == "login"
    me = env["c"].get("/api/v1/agents/me", headers=env["claude"]).json()
    assert me["blocked"]["host"] == "box" and me["blocked"]["task_nr"] == "KET-0001"
    since = me["blocked"]["since"]
    # Idempotent: the same kind keeps `since`.
    r = _report(env, reason="still expired", since="2026-01-01T00:00:00+00:00")
    assert r.json()["new"] is False and r.json()["blocked"]["since"] == since
    assert r.json()["blocked"]["reason"] == "still expired"
    # Humans cannot report; unknown kinds are refused.
    assert _report(env, who="admin").status_code == 403
    assert _report(env, kind="nonsense").status_code == 422
    r = env["c"].delete("/api/v1/agents/me/blocked", headers=env["claude"])
    assert r.status_code == 200 and r.json()["cleared"] is True
    assert env["c"].get("/api/v1/agents/me", headers=env["claude"]).json()["blocked"] is None
    assert env["c"].delete("/api/v1/agents/me/blocked", headers=env["claude"]).json()["cleared"] is False


@pytest.mark.db
def test_clear_sends_ready_notification(env):
    mkproject(env)
    mktask(env, assigned_to="claude-code")
    _link()
    _report(env)
    env["c"].delete("/api/v1/agents/me/blocked", headers=env["claude"])
    with db.session() as s:
        rows = list(s.scalars(select(Notification).where(Notification.event == "agent_ready")))
    # Server default language in the tests: German.
    assert len(rows) == 1 and "claude-code · Agent wieder bereit" in rows[0].text
    assert "Wartende Tasks: 1" in rows[0].text and "claude /login" in rows[0].text


@pytest.mark.db
def test_admin_clears_in_ui_and_inbox_section(env, ui):  # noqa: F811
    mkproject(env)
    sid = env["c"].post("/api/v1/projects/KET/sprints", headers=env["admin"], json={"name": "Wave"}).json()["id"]
    a = mktask(env, assigned_to="claude-code", sprint=sid)["task_nr"]
    b = mktask(env, assigned_to="codex", depends_on=[a], sprint=sid)["task_nr"]
    c = mktask(env, assigned_to="codex", depends_on=[b], sprint=sid)["task_nr"]
    assert env["c"].post(f"/api/v1/projects/KET/sprints/{sid}/release", headers=env["admin"]).status_code == 200
    _report(env, task_nr=a)
    page = ui.get("/").text
    assert "Blockierte Agenten" in page and "claude /login" in page and "Host: box" in page  # UI language de
    assert "3 wartende Tasks" in page and "1 zugewiesen, 2 über Abhängigkeiten" in page
    # Badges: the agent's own task and tasks waiting through dependencies (also indirectly).
    detail = ui.get(f"/tasks/{a}").text
    assert "claude-code blockiert: Login abgelaufen seit" in detail
    assert f"wartet auf {a} – Agent blockiert" in ui.get(f"/tasks/{c}").text
    board = ui.get("/kanban").text
    assert f"wartet auf {a} – Agent blockiert" in board and "claude-code blockiert: Login abgelaufen" in board
    assert f"wartet auf {a} – Agent blockiert" in ui.get("/backlog").text
    planning = ui.get("/planning?project=KET").text
    assert f"wartet auf {a} – Agent blockiert" in planning and "claude-code blockiert" in planning
    users = ui.get("/admin/users?all_agents=1").text
    assert "Blockiert: Login abgelaufen" in users
    r = post(ui, "/admin/users/claude-code/unblock?back=inbox")
    assert r.status_code == 200 and "Blockierte Agenten" not in r.text
    with db.session() as s:
        assert s.scalar(select(Actor).where(Actor.name == "claude-code")).blocked_since is None
    assert "Agent blockiert" not in ui.get("/kanban").text


@pytest.mark.db
def test_inbox_section_only_for_concerned_humans(env):
    mkproject(env)
    with db.session() as s:
        agent = s.scalar(select(Actor).where(Actor.name == "claude-code"))
        stranger = s.scalar(select(Actor).where(Actor.actor_type == "human", Actor.role != "admin"))
        admin = s.scalar(select(Actor).where(Actor.name == "admin"))
        agent_blocks.report(s, agent, "limit", "usage limit reached", "box", None)
        s.flush()
        assert [b["name"] for b in agent_blocks.visible_for(s, admin)] == ["claude-code"]
        if stranger is not None:
            assert agent_blocks.visible_for(s, stranger) == []
            agent.owner_id = stranger.id
            s.flush()
            entry = agent_blocks.visible_for(s, stranger)
            assert entry and entry[0]["can_clear"] is False and entry[0]["waiting"] == 0
        s.rollback()


@pytest.mark.db
def test_reminder_scheduling(env):
    mkproject(env)
    mktask(env, assigned_to="claude-code")
    _link()
    _report(env)
    with db.session() as s:
        agent = s.scalar(select(Actor).where(Actor.name == "claude-code"))
        since = agent.blocked_since
        assert agent_blocks.remind(s, since + timedelta(hours=3, minutes=59)) == {}
        assert agent_blocks.remind(s, since + timedelta(hours=4)) == {"reminded": 1}
        assert agent.blocked_reminded_at == since + timedelta(hours=4)
        # The next one counts from the last reminder.
        assert agent_blocks.remind(s, since + timedelta(hours=7)) == {}
        assert agent_blocks.remind(s, since + timedelta(hours=8)) == {"reminded": 1}
        rows = list(s.scalars(select(Notification).where(Notification.event == "agent_blocked")))
        assert len(rows) == 2 and "weiterhin blockiert: Login abgelaufen" in rows[0].text
        assert "auf box" in rows[0].text and rows[0].task_nr == "KET-0001"
        # Interval from Admin → Settings; 0 switches reminders off.
        settings_store.update(s, {"agent_block_reminder_hours": "0"}, None)
        assert agent_blocks.remind(s, since + timedelta(days=3)) == {}
        settings_store.update(s, {"agent_block_reminder_hours": "1"}, None)
        assert agent_blocks.remind(s, since + timedelta(hours=9)) == {"reminded": 1}
        s.commit()


@pytest.mark.db
def test_reminder_honours_mute(env):
    mkproject(env)
    _link()
    _report(env)
    with db.session() as s:
        admin = s.scalar(select(Actor).where(Actor.name == "admin"))
        notifications.save_prefs(s, admin, mute_all=notifications.FOREVER)
        agent = s.scalar(select(Actor).where(Actor.name == "claude-code"))
        agent.owner_id = admin.id
        agent_blocks.remind(s, agent.blocked_since + timedelta(hours=5))
        rows = list(s.scalars(select(Notification).where(Notification.event == "agent_blocked")))
        assert rows and all(r.status == "skipped" and r.last_error == "muted" for r in rows)
        s.rollback()
