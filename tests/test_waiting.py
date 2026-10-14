"""Conscious waiting for long background runs: `tbr heartbeat --waiting`, server state, runner stall detection."""

import pytest
from sqlalchemy import text

from cli import runner
from cli.client import heartbeat_task, parse_duration, waiting_report
from tests.test_runner import Clock, FakeTmux, RunnerClient, _supervisor, task
from tests.test_workflow import claim, env, gitea, mkproject, mktask  # noqa: F401


# --- CLI helpers (no DB) --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,minutes", [("90", 90), ("45m", 45), ("2h", 120), ("1h30m", 90), ("1d", 1440),
                                           (None, None), ("", None)])
def test_parse_duration(value, minutes):
    assert parse_duration(value) == minutes


@pytest.mark.parametrize("value", ["abc", "0", "h", "-5m"])
def test_parse_duration_invalid(value):
    with pytest.raises(ValueError):
        parse_duration(value)


class Recorder:
    def __init__(self):
        self.calls = []

    def request(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json")))

        class R:
            @staticmethod
            def json():
                return {"task_nr": "KET-0001", "lease_expires_at": "2026-10-12T10:00:00", "waiting": None}
        return R()


def test_heartbeat_task_bodies(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rec = Recorder()
    heartbeat_task(rec, "ket-0001")
    heartbeat_task(rec, "KET-0001", "model test", "2h", "run.log")
    heartbeat_task(rec, "KET-0001", done_waiting=True)
    assert rec.calls[0] == ("POST", "/tasks/KET-0001/heartbeat", None)
    assert rec.calls[1][2] == {"waiting": "model test", "for_minutes": 120, "watch_file": str(tmp_path / "run.log")}
    assert rec.calls[2][2] == {"done_waiting": True}
    with pytest.raises(ValueError):
        heartbeat_task(rec, "KET-0001", duration="2h")  # --for needs --waiting
    with pytest.raises(ValueError):
        heartbeat_task(rec, "KET-0001", "x", done_waiting=True)


def test_waiting_report():
    out = waiting_report({"task_nr": "KET-0001", "lease_expires_at": "L",
                          "waiting": {"reason": "model test", "until": "U"}})
    assert "Lease of KET-0001 until L" in out and "model test" in out and "until U" in out


# --- Runner (no DB) -------------------------------------------------------------------------------------------------

class WaitingClient(RunnerClient):
    waiting = None

    def get(self, path):
        out = super().get(path)
        return {**out, "waiting": self.waiting} if path.startswith("/tasks/") else out


def _stall_setup(tmp_path, monkeypatch, waiting=None, **cfg):
    monkeypatch.setattr(runner, "_tmux", FakeTmux(pane="idle"))
    clock = Clock()
    fc = WaitingClient([task("KET-0030", "b", "x")])
    fc.waiting = waiting
    cfg.setdefault("park_idle_seconds", 0)  # The session stays open while waiting (parking: see below).
    sup = _supervisor(fc, tmp_path, clock=clock, stall_minutes=30, **cfg)
    comments = lambda: [c for c in fc.calls if c[1] == "/tasks/KET-0030/comments"]  # noqa: E731
    return sup, fc, clock, comments


def test_no_stall_note_while_waiting(tmp_path, monkeypatch):
    sup, fc, clock, comments = _stall_setup(tmp_path, monkeypatch, waiting={"reason": "model test", "until": "x"})
    for _step in range(120):  # 2 h
        clock.t += 60
        assert sup.check() is None
    assert comments() == []


def test_stall_detection_returns_after_waiting_ended(tmp_path, monkeypatch):
    sup, fc, clock, comments = _stall_setup(tmp_path, monkeypatch, waiting={"reason": "r", "until": "x"})
    for _step in range(120):
        clock.t += 60
        sup.check()
    fc.waiting = None  # Expired without being extended.
    for _step in range(29):
        clock.t += 60
        sup.check()
    assert comments() == []
    for _step in range(3):
        clock.t += 60
        sup.check()
    assert len(comments()) == 1 and "No activity in the agent window" in comments()[0][2]["body"]



def test_idle_agent_is_stopped_and_parked_while_waiting(tmp_path, monkeypatch):
    sup, fc, clock, comments = _stall_setup(tmp_path, monkeypatch, waiting={"reason": "model test", "until": "x"},
                                            park_idle_seconds=120, start_check_seconds=0)
    for _step in range(2):  # First capture, then 1 min unchanged.
        clock.t += 60
        assert sup.check() is None
    clock.t += 60
    msg, kind = sup.check()  # Window unchanged for 2 min while waiting: end the session, keep the task.
    assert "waits for a background run and the agent is idle" in msg and kind is None
    assert not [c for c in fc.calls if c[1].endswith(("/release", "/submit"))] and comments() == []


def test_no_parking_without_waiting(tmp_path, monkeypatch):
    sup, fc, clock, comments = _stall_setup(tmp_path, monkeypatch, park_idle_seconds=120, start_check_seconds=0)
    for _step in range(10):
        clock.t += 60
        assert sup.check() is None

def test_watch_file_unchanged_is_reported_once(tmp_path, monkeypatch):
    log = tmp_path / "run.log"
    log.write_text("epoch 1\nepoch 2\n")
    sup, fc, clock, comments = _stall_setup(tmp_path, monkeypatch,
                                            waiting={"reason": "r", "until": "x", "watch_file": str(log)})
    for _step in range(29):
        clock.t += 60
        sup.check()
    assert comments() == []
    for _step in range(5):
        clock.t += 60
        sup.check()
    assert len(comments()) == 1
    body = comments()[0][2]["body"]
    assert comments()[0][2]["signal"] == "stalled"
    assert f"Background run is no longer writing: {log}" in body and "epoch 2" in body
    assert "agent window" not in body
    for _step in range(60):
        clock.t += 60
        sup.check()
    assert len(comments()) == 1  # Once per silence.


def test_watch_file_change_counts_as_activity(tmp_path, monkeypatch):
    log = tmp_path / "run.log"
    log.write_text("a\n")
    sup, fc, clock, comments = _stall_setup(tmp_path, monkeypatch,
                                            waiting={"reason": "r", "until": "x", "watch_file": str(log)})
    for step in range(200):
        clock.t += 60
        if step % 20 == 0:
            log.write_text(log.read_text() + "more\n")  # Grows every 20 min: size changes.
        sup.check()
    assert comments() == []


def test_missing_watch_file_is_reported(tmp_path, monkeypatch):
    sup, fc, clock, comments = _stall_setup(
        tmp_path, monkeypatch, waiting={"reason": "r", "until": "x", "watch_file": str(tmp_path / "nope.log")})
    for _step in range(31):
        clock.t += 60
        sup.check()
    assert len(comments()) == 1 and "(file not found)" in comments()[0][2]["body"]


# --- Server (DB) ----------------------------------------------------------------------------------------------------

def _hb(e, nr, **body):
    return e["c"].post(f"/api/v1/tasks/{nr}/heartbeat", headers=e["claude"], json=body or None)


def _task(e, nr):
    return e["c"].get(f"/api/v1/tasks/{nr}", headers=e["admin"]).json()


@pytest.mark.db
def test_waiting_state_lifecycle(env):  # noqa: F811
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env)
    assert _task(env, nr)["waiting"] is None
    r = _hb(env, nr, waiting="model test", for_minutes=180, watch_file="/data/run.log")
    assert r.status_code == 200 and r.json()["waiting"]["reason"] == "model test"
    w = _task(env, nr)["waiting"]
    assert w["reason"] == "model test" and w["watch_file"] == "/data/run.log" and w["until"]
    assert _hb(env, nr).json()["waiting"]["reason"] == "model test"  # A plain heartbeat (runner) keeps it.
    assert _hb(env, nr, done_waiting=True).json()["waiting"] is None
    assert _task(env, nr)["waiting"] is None


@pytest.mark.db
def test_waiting_default_cap_and_extension(env):  # noqa: F811
    from datetime import UTC, datetime

    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env)

    def minutes():
        until = datetime.fromisoformat(_task(env, nr)["waiting"]["until"]).replace(tzinfo=None)
        return (until - datetime.now(UTC).replace(tzinfo=None)).total_seconds() / 60

    _hb(env, nr, waiting="x")
    assert 118 < minutes() <= 120  # Default 2 h.
    _hb(env, nr, waiting="x", for_minutes=100000)
    assert 24 * 60 - 2 < minutes() <= 24 * 60  # Cap 24 h; a new call extends.


@pytest.mark.db
def test_waiting_expires_and_ends_with_ask(env):  # noqa: F811
    from app.db import db

    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env)
    _hb(env, nr, waiting="x")
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE tasks SET waiting_until = UTC_TIMESTAMP() - INTERVAL 1 MINUTE WHERE task_nr = :n"),
                     {"n": nr})
    assert _task(env, nr)["waiting"] is None  # Expired: normal detection.
    _hb(env, nr, waiting="again")
    r = env["c"].post(f"/api/v1/tasks/{nr}/ask", headers=env["claude"], json={"body": "Question?"})
    assert r.status_code == 200
    with db.engine.begin() as conn:
        assert conn.execute(text("SELECT waiting_reason FROM tasks WHERE task_nr = :n"), {"n": nr}).scalar() is None


@pytest.mark.db
def test_waiting_only_for_the_assignee(env):  # noqa: F811
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env)
    r = env["c"].post(f"/api/v1/tasks/{nr}/heartbeat", headers=env["hermes"], json={"waiting": "x"})
    assert r.status_code == 403 and _task(env, nr)["waiting"] is None


@pytest.mark.db
def test_waiting_shown_in_detail(env):  # noqa: F811
    from tests.conftest import login

    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    claim(env)
    _hb(env, nr, waiting="model test")
    assert login(env["c"]).status_code in (200, 303)
    page = env["c"].get(f"/tasks/{nr}")
    assert page.status_code == 200 and "Wartet auf Hintergrundlauf: model test (bis " in page.text


def test_waiting_shown_on_kanban_card():
    from datetime import UTC, datetime

    from tests.test_kanban import card, render_board

    waiting = card("KET-0001", "in_progress") | {"waiting": {"reason": "model test", "until": datetime(2026, 10, 12, 20, tzinfo=UTC)}}
    html = render_board([waiting, card("KET-0002", "in_progress")], "de")
    assert html.count("Wartet auf Hintergrundlauf: model test (bis ") == 1
    assert "Waiting for a background run: model test (until " in render_board([waiting], "en")
