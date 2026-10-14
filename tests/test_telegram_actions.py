"""Telegram 2: review buttons (approve with confirmation, reject with a reason, return conflict), answers by reply,
bot commands. The Bot API is faked (`FakeApi` from `tests/test_telegram.py`)."""
import logging
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.db import db
from app.models import Actor, QuestionRead, TaskComment, TelegramAction
from app.services import notifications, telegram_actions, telegram_bot
from app.services.actors import utcnow
from tests.gitea_fake import SERVICE_USER
from tests.test_telegram import _task, ask, deliver, fake, link, rows, start, tenv  # noqa: F401 (Fixtures)
from tests.test_ui_cli import ui  # noqa: F401 (Fixture)
from tests.test_workflow import SHA, claim, env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)


def press(chat_id, data, cb_id="cb1", message_id=500):
    return {"update_id": 9, "callback_query": {"id": cb_id, "from": {"id": chat_id}, "data": data,
                                               "message": {"message_id": message_id, "chat": {"id": chat_id}}}}


def reply(chat_id, text, to=None):
    upd = start(chat_id, text)
    if to is not None:
        upd["message"]["reply_to_message"] = {"message_id": to}
    return upd


def buttons(markup):
    return {b["text"]: b["callback_data"] for row in (markup or {}).get("inline_keyboard", []) for b in row}


def handle(upd):
    """Run one update through the poller thread's handler (DB session, answer, edit, send)."""
    telegram_bot.BotThread().handle(_api(), upd)


def _api():
    from app.services import telegram

    return telegram.api_factory("x")


def say(chat_id, text, to=None):
    with db.session() as s:
        out = telegram_bot.handle_update(s, reply(chat_id, text, to))
        s.commit()
        return out


def status(env, nr):
    return env["c"].get(f"/api/v1/tasks/{nr}", headers=env["admin"]).json()["status"]


def for_review(env, project="KET", who="claude", agent="claude-code", repo="ket"):
    nr = mktask(env, project=project, assigned_to=agent, title="Login bauen")["task_nr"]
    branch = claim(env, who=who, task_nr=nr).json()["branch_name"]
    env["gitea"].push_branch(SERVICE_USER, repo, branch)
    r = env["c"].post(f"/api/v1/tasks/{nr}/submit", headers=env[who],
                      json={"branch_name": branch, "commit_sha": SHA, "summary": "Geheime Zusammenfassung"})
    assert r.status_code == 200, r.text
    return nr


def audits(caplog, action):
    return [r for r in caplog.records if getattr(r, "audit_action", None) == action]


# --- Without a database ---------------------------------------------------------------------------

def test_parse_until(settings, monkeypatch):
    monkeypatch.setattr(settings, "tz_display", "Europe/Berlin")  # UTC+2 in October
    now = datetime(2026, 10, 10, 10, 0)  # 12:00 local
    assert notifications.parse_until("2h", now) == datetime(2026, 10, 10, 12, 0)
    assert notifications.parse_until("bis 18:00", now) == datetime(2026, 10, 10, 16, 0)
    assert notifications.parse_until("until 9:30", now) == datetime(2026, 10, 11, 7, 30)  # Tomorrow
    assert notifications.parse_until("18.00 Uhr", now) == datetime(2026, 10, 10, 16, 0)
    assert notifications.parse_until("bis 25:00", now) is None and notifications.parse_until("bald", now) is None


def test_callback_from_unlinked_chat_is_ignored():
    class NoSession:
        def scalar(self, *a):
            return None

    assert telegram_bot.handle_callback(NoSession(), press(5, "tb:" + "a" * 22)["callback_query"]) is None


# --- Buttons --------------------------------------------------------------------------------------

@pytest.mark.db
def test_approve_asks_for_confirmation_and_double_clicks_do_nothing(tenv, caplog):
    caplog.set_level(logging.INFO, logger="admin_audit")
    fake = tenv["fake"]
    link("admin", 100)
    nr = for_review(tenv)
    deliver()
    review = buttons(fake.markups[-1])
    assert set(review) == {"✅ Abnehmen", "↩ Zurückweisen"} and all(len(d) <= 64 for d in review.values())
    with db.session() as s:  # Only the hash of the token is stored.
        assert review["✅ Abnehmen"][3:] not in {a.token_hash for a in s.scalars(select(TelegramAction))}

    handle(press(100, review["✅ Abnehmen"]))
    assert fake.answers[-1] == ("cb1", "Bitte den Merge bestätigen.")
    confirm = buttons(fake.edits[-1][2])
    assert set(confirm) == {f"✅ Ja, {nr} mergen", "✖ Abbrechen"} and status(tenv, nr) == "for_review"
    handle(press(100, review["✅ Abnehmen"], "cb2"))  # Double click on the first button
    assert fake.answers[-1] == ("cb2", "Dieser Knopf wurde bereits verwendet.") and len(fake.edits) == 1

    handle(press(100, confirm[f"✅ Ja, {nr} mergen"], "cb3"))
    assert status(tenv, nr) == "done"
    assert fake.answers[-1] == ("cb3", "Gemergt.") and fake.edits[-1][2] is None  # Buttons removed
    assert fake.sent[-1] == (100, f"{nr} gemerged und erledigt.\nhttps://tm.example/tasks/{nr}")
    sent = len(fake.sent)
    handle(press(100, confirm[f"✅ Ja, {nr} mergen"], "cb4"))  # Double click: nothing happens again
    assert fake.answers[-1] == ("cb4", f"Bereits erledigt: {nr} gemerged und erledigt.") and len(fake.sent) == sent
    (entry,) = audits(caplog, "review_approve")
    assert entry.audit_actor == "admin" and entry.via == "telegram" and entry.task == nr and entry.outcome == "ok"


@pytest.mark.db
def test_cancel_restores_the_review_buttons(tenv):
    fake = tenv["fake"]
    link("admin", 100)
    nr = for_review(tenv)
    deliver()
    handle(press(100, buttons(fake.markups[-1])["✅ Abnehmen"]))
    handle(press(100, buttons(fake.edits[-1][2])["✖ Abbrechen"], "cb2"))
    assert fake.answers[-1] == ("cb2", "Abgebrochen.") and status(tenv, nr) == "for_review"
    assert set(buttons(fake.edits[-1][2])) == {"✅ Abnehmen", "↩ Zurückweisen"}


@pytest.mark.db
def test_reject_asks_for_the_reason_and_stores_it_as_review_comment(tenv, caplog):
    caplog.set_level(logging.INFO, logger="admin_audit")
    fake = tenv["fake"]
    link("admin", 100)
    nr = for_review(tenv)
    deliver()
    handle(press(100, buttons(fake.markups[-1])["↩ Zurückweisen"]))
    prompt, markup = fake.sent[-1][1], fake.markups[-1]
    assert prompt.startswith(f"Warum wird {nr} zurückgewiesen?") and markup["force_reply"]
    assert set(buttons(fake.edits[-1][2])) == {"✖ Zurückweisen abbrechen"}
    assert say(100, "Tests fehlen") == f"{nr} zurückgewiesen.\nhttps://tm.example/tasks/{nr}"
    assert status(tenv, nr) == "changes_requested"
    body = tenv["c"].get(f"/api/v1/tasks/{nr}", headers=tenv["admin"]).json()["comments"][-1]
    assert body["type"] == "review" and body["body"] == "Tests fehlen"
    assert "versteht hier nur Befehle" in say(100, "noch was")  # Nothing pending any more
    assert audits(caplog, "review_reject")[0].via == "telegram"


@pytest.mark.db
def test_pending_rejection_can_be_cancelled(tenv):
    fake = tenv["fake"]
    link("admin", 100)
    nr = for_review(tenv)
    deliver()
    handle(press(100, buttons(fake.markups[-1])["↩ Zurückweisen"]))
    assert say(100, "/abbrechen") == "Zurückweisen abgebrochen."
    assert say(100, "/cancel") == "Nichts abzubrechen."
    assert "versteht hier nur Befehle" in say(100, "Tests fehlen") and status(tenv, nr) == "for_review"


@pytest.mark.db
def test_buttons_check_rights_owner_and_chat(tenv):
    link("admin", 100)
    link("alex", 200)
    nr = for_review(tenv)  # KET: owner admin; alex is a plain member
    with db.session() as s:
        alex = s.scalar(select(Actor).where(Actor.name == "alex"))
        admin = s.scalar(select(Actor).where(Actor.name == "admin"))
        foreign = buttons(telegram_actions.review_keyboard(s, alex.id, nr))
        admins = buttons(telegram_actions.review_keyboard(s, admin.id, nr))
        s.commit()
    with db.session() as s:
        result = telegram_bot.handle_callback(s, press(200, foreign["✅ Abnehmen"])["callback_query"])
        assert result.notice.startswith(f"{nr}: approve: ") and result.markup is None
        # Admin's button pressed in alex's chat: not valid there.
        result = telegram_bot.handle_callback(s, press(200, admins["✅ Abnehmen"])["callback_query"])
        assert result.notice == "Dieser Knopf ist ungültig."
        # Guessed or forged data.
        for data in ("tb:" + "A" * 22, "approve:" + nr, "tb:../../x"):
            assert telegram_bot.handle_callback(s, press(100, data)["callback_query"]).notice == \
                "Dieser Knopf ist ungültig."
        s.commit()
    assert status(tenv, nr) == "for_review"


@pytest.mark.db
def test_expired_button(tenv):
    link("admin", 100)
    nr = for_review(tenv)
    with db.session() as s:
        admin = s.scalar(select(Actor).where(Actor.name == "admin"))
        b = buttons(telegram_actions.review_keyboard(s, admin.id, nr))
        for a in s.scalars(select(TelegramAction)):
            a.expires_at = utcnow() - timedelta(seconds=1)
        s.commit()
    with db.session() as s:
        result = telegram_bot.handle_callback(s, press(100, b["✅ Abnehmen"])["callback_query"])
        assert result.notice == "Dieser Knopf ist abgelaufen." and "abgelaufen" in result.messages[0][0]


@pytest.mark.db
def test_merge_conflict_explained_and_returned_from_telegram(tenv, caplog):
    caplog.set_level(logging.INFO, logger="admin_audit")
    fake = tenv["fake"]
    link("admin", 100)
    nr = for_review(tenv)
    deliver()
    tenv["gitea"].conflict = {"pr_files": ["brain.md"], "base_files": ["brain.md"]}
    handle(press(100, buttons(fake.markups[-1])["✅ Abnehmen"]))
    handle(press(100, buttons(fake.edits[-1][2])[f"✅ Ja, {nr} mergen"], "cb2"))
    assert fake.answers[-1] == ("cb2", "Nicht gemergt.") and status(tenv, nr) == "for_review"
    text, markup = fake.sent[-1][1], fake.markups[-1]
    assert text.startswith(f"{nr}: Merge-Konflikt mit main") and "Mit dem Knopf" in text
    # The conflict notification (stored although the approval rolled back) carries the button too.
    assert [n.event for n in rows()][-1] == "conflict"
    handle(press(100, buttons(markup)["↩ Konflikt zurückgeben"], "cb3"))
    assert fake.answers[-1] == ("cb3", "Zurückgegeben.") and status(tenv, nr) == "changes_requested"
    assert fake.sent[-1][1].startswith(f"{nr} mit Konflikt-Anleitung an den Agenten zurückgegeben.")
    assert [r.outcome for r in audits(caplog, "review_approve")] == ["merge_conflict"]
    assert [r.outcome for r in audits(caplog, "review_reject_conflict")] == ["ok"]


@pytest.mark.db
def test_local_only_replies_reveal_no_content(tenv):
    fake = tenv["fake"]
    link("alex", 200)
    nr = for_review(tenv, project="OWN", who="hermes", agent="hermes", repo="own")
    deliver()
    assert "Geheime" not in fake.sent[-1][1]
    tenv["gitea"].conflict = {"pr_files": ["brain.md"], "base_files": ["brain.md"]}
    handle(press(200, buttons(fake.markups[-1])["✅ Abnehmen"]))
    handle(press(200, buttons(fake.edits[-1][2])[f"✅ Ja, {nr} mergen"], "cb2"))
    text = fake.sent[-1][1]
    assert text.startswith(f"{nr}: Merge-Konflikt – kein Merge, der Task bleibt im Review.")
    assert "main" not in text and "brain.md" not in text and "hermes" not in text and "Login" not in text


# --- Answers by reply -----------------------------------------------------------------------------

@pytest.mark.db
def test_reply_to_question_answers_in_the_thread(tenv, caplog):
    caplog.set_level(logging.INFO, logger="admin_audit")
    fake = tenv["fake"]
    link("admin", 100)
    nr = mktask(tenv, assigned_to="claude-code")["task_nr"]
    claim(tenv, task_nr=nr)
    ask(tenv, nr)
    deliver()
    assert "Auf diese Nachricht antworten" in fake.sent[-1][1]
    (n,) = rows()
    assert n.telegram_message_id == 1001
    assert say(100, "MariaDB 10.5", to=1001) == f"{nr} beantwortet – wieder abholbar.\n" \
                                                f"https://tm.example/tasks/{nr}"
    task = tenv["c"].get(f"/api/v1/tasks/{nr}", headers=tenv["admin"]).json()
    assert task["status"] == "defined" and task["comments"][-1]["type"] == "answer"
    assert task["comments"][-1]["body"] == "MariaDB 10.5" and task["comments"][-1]["author"] == "admin"
    assert say(100, "nochmal", to=1001).startswith(f"{nr} hat keine offene Rückfrage mehr.")
    assert "versteht hier nur Befehle" in say(100, "Antwort ohne Bezug", to=4711)
    assert audits(caplog, "question_answered")[0].via == "telegram"


@pytest.mark.db
def test_reply_to_an_old_question_does_not_answer_a_newer_one(tenv):
    link("admin", 100)
    nr = mktask(tenv, assigned_to="claude-code")["task_nr"]
    claim(tenv, task_nr=nr)
    ask(tenv, nr)
    deliver()
    with db.session() as s:  # The agent asked again later (answered in the browser meanwhile).
        q = s.scalar(select(TaskComment).where(TaskComment.comment_type == "question"))
        q.created_at = utcnow() + timedelta(minutes=5)
        s.commit()
    assert "neuere Rückfrage" in say(100, "Antwort", to=1001) and status(tenv, nr) == "questions"


@pytest.mark.db
def test_local_only_question_needs_reading_in_the_browser(tenv, ui):  # noqa: F811
    mkproject(tenv, key="LOC", privacy="local_only")  # Owner admin
    link("admin", 100)
    nr = mktask(tenv, project="LOC", assigned_to="hermes")["task_nr"]
    claim(tenv, who="hermes", task_nr=nr)
    ask(tenv, nr, who="hermes", text="Geheime Frage")
    deliver()
    assert "Geheime" not in tenv["fake"].sent[-1][1]
    out = say(100, "Ja", to=1001)
    assert "local-only-Projekt" in out and f"https://tm.example/tasks/{nr}" in out
    assert status(tenv, nr) == "questions"
    assert ui.get(f"/tasks/{nr}").status_code == 200  # Read in the browser
    with db.session() as s:
        assert s.scalar(select(QuestionRead)) is not None
    assert say(100, "Ja", to=1001).startswith(f"{nr} beantwortet")
    assert status(tenv, nr) == "defined"


@pytest.mark.db
def test_inbox_page_counts_as_read(tenv, ui):  # noqa: F811
    mkproject(tenv, key="LOC", privacy="local_only")
    nr = mktask(tenv, project="LOC", assigned_to="hermes")["task_nr"]
    claim(tenv, who="hermes", task_nr=nr)
    ask(tenv, nr, who="hermes")
    assert ui.get("/").status_code == 200
    with db.session() as s:
        assert len(s.scalars(select(QuestionRead)).all()) == 1


# --- Commands -------------------------------------------------------------------------------------

@pytest.mark.db
def test_inbox_command_lists_what_waits_without_local_content(tenv):
    link("alex", 200)
    link("admin", 100)
    assert say(200, "/inbox") == "Nichts wartet auf dich."
    ket = mktask(tenv, assigned_to="claude-code", title="Login bauen")["task_nr"]
    claim(tenv, task_nr=ket)
    ask(tenv, ket)
    own = for_review(tenv, project="OWN", who="hermes", agent="hermes", repo="own")
    alex = say(200, "/inbox")
    assert alex.startswith("Reviews (1)") and f"• {own}\n  https://tm.example/tasks/{own}" in alex
    assert "Login" not in alex and ket not in alex  # local_only: no title; KET is not alex's project
    admin = say(100, "/inbox")
    assert admin.startswith("Rückfragen (1)\n• " + ket + " Login bauen") and own not in admin
    with db.session() as s:
        notifications.save_prefs(s, s.scalar(select(Actor).where(Actor.name == "admin")), all_projects=True)
        s.commit()
    assert own in say(100, "/inbox") and "Inbox: https://tm.example/" in say(100, "/inbox")


@pytest.mark.db
def test_mute_commands_in_both_languages(tenv, settings, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="admin_audit")
    monkeypatch.setattr(settings, "tz_display", "UTC")
    link("admin", 100)

    def pref():
        with db.session() as s:
            return notifications.prefs(s, s.scalar(select(Actor.id).where(Actor.name == "admin")))

    assert "stummgeschaltet bis zum Aufheben" in say(100, "/stumm")
    assert pref().mute_all_until == notifications.FOREVER
    assert "wieder eingeschaltet" in say(100, "/laut") and pref().mute_all_until is None
    say(100, "/pause 2h")
    assert timedelta(hours=1, minutes=59) < pref().mute_all_until - utcnow() <= timedelta(hours=2)
    say(100, "/resume")
    say(100, "/pause bis 23:59")
    assert pref().mute_all_until.strftime("%H:%M") == "23:59"
    say(100, "/unmute")
    assert "Unbekannte Dauer" in say(100, "/stumm irgendwann") and pref().mute_all_until is None
    assert "Review-Aufforderungen stummgeschaltet" in say(100, "/reviews aus")
    assert pref().mute_review_until == notifications.FOREVER and pref().mute_all_until is None
    assert "/reviews aus" in say(100, "/reviews")
    assert "wieder eingeschaltet" in say(100, "/reviews an") and pref().mute_review_until is None
    say(100, "/reviews off 1d")
    assert pref().mute_review_until > utcnow() + timedelta(hours=23)
    assert {r.audit_action for r in caplog.records if getattr(r, "via", None) == "telegram"} >= {
        "notifications_muted", "notifications_unmuted"}


@pytest.mark.db
def test_status_and_help(tenv):
    link("admin", 100)
    out = say(100, "/status")
    assert out.startswith("Verknüpft mit") and "Server: https://tm.example" in out
    for command in ("/hilfe", "/help", "/unbekannt"):
        assert "/inbox" in say(100, command) and "/reviews aus|an" in say(100, command)
    assert say(999, "/hilfe") is None  # Not linked


@pytest.mark.db
def test_command_menu_is_registered_on_a_new_token(tenv, monkeypatch):
    from app.db import db as database

    thread = telegram_bot.BotThread()
    monkeypatch.setattr(thread, "acquire_lock", lambda: True)
    monkeypatch.setattr(type(database), "ready", property(lambda self: True))
    assert thread.step() == 0.0
    assert [c for c, _d in tenv["fake"].commands] == ["inbox", "stumm", "laut", "reviews", "status", "hilfe"]
