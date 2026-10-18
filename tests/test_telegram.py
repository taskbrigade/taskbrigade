"""Telegram: bot settings, chat linking, notification events, per-person settings and delivery.
The Bot API is always faked (`FakeApi` replaces `telegram.api_factory`); no test talks to Telegram."""
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.db import db
from app.models import Actor, AppSetting, Notification, TelegramLink, TelegramLinkCode
from app.services import notifications, settings_store, telegram, telegram_bot
from app.services.actors import utcnow
from tests.conftest import bearer, make_token
from tests.gitea_fake import SERVICE_USER
from tests.test_ui_cli import post, ui  # noqa: F401 (Fixtures)
from tests.test_workflow import SHA, claim, env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)

TOKEN = "123456:secret-bot-token"


class FakeApi:
    """Records calls; `fail` is an exception raised by the next send_message calls."""

    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.markups: list[dict | None] = []  # reply_markup per sent message
        self.fail: Exception | None = None
        self.updates: list[dict] = []
        self.tokens: list[str] = []
        self.answers: list[tuple[str, str | None]] = []
        self.edits: list[tuple[int, int, dict | None]] = []
        self.commands: list[tuple[str, str]] = []

    def __call__(self, token):  # Used as api_factory.
        self.tokens.append(token)
        return self

    def get_me(self):
        return {"id": 42, "username": "tb_test_bot"}

    def send_message(self, chat_id, text, reply_markup=None, reply_to=None):
        if self.fail is not None:
            raise self.fail
        self.sent.append((chat_id, text))
        self.markups.append(reply_markup)
        return {"message_id": 1000 + len(self.sent), "chat": {"id": chat_id}}

    def answer_callback(self, callback_id, text=None):
        self.answers.append((callback_id, text))
        return True

    def edit_reply_markup(self, chat_id, message_id, reply_markup):
        self.edits.append((chat_id, message_id, reply_markup))
        return {}

    def set_commands(self, commands):
        self.commands = commands
        return True

    def get_updates(self, offset, timeout=25):
        out, self.updates = self.updates, []
        return out


@pytest.fixture
def fake(monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(telegram, "api_factory", api)
    return api


def start(chat_id, text, username="alex_tg"):
    return {"update_id": 1, "message": {"chat": {"id": chat_id, "type": "private", "username": username},
                                        "text": text}}


# --- Without a database ---------------------------------------------------------------------------

def test_parse_duration():
    assert notifications.parse_duration("30m") == timedelta(minutes=30)
    assert notifications.parse_duration("2 h") == timedelta(hours=2)
    assert notifications.parse_duration("1d") == timedelta(days=1)
    assert notifications.parse_duration("1w") == timedelta(weeks=1)
    assert notifications.parse_duration("bald") is None and notifications.parse_duration("0h") is None


def test_quiet_hours_span_midnight(settings, monkeypatch):
    monkeypatch.setattr(settings, "tz_display", "UTC")
    pref = notifications.NotificationPref(quiet_start=22 * 60, quiet_end=7 * 60)
    assert notifications.quiet_until(pref, datetime(2026, 10, 10, 21, 59)) is None
    assert notifications.quiet_until(pref, datetime(2026, 10, 10, 23, 0)) == datetime(2026, 10, 11, 7, 0)
    assert notifications.quiet_until(pref, datetime(2026, 10, 11, 3, 0)) == datetime(2026, 10, 11, 7, 0)
    assert notifications.quiet_until(pref, datetime(2026, 10, 11, 7, 0)) is None
    day = notifications.NotificationPref(quiet_start=12 * 60, quiet_end=13 * 60)
    assert notifications.quiet_until(day, datetime(2026, 10, 10, 12, 30)) == datetime(2026, 10, 10, 13, 0)
    assert notifications.quiet_until(notifications.NotificationPref(), datetime(2026, 10, 10, 3, 0)) is None


def test_quiet_hours_use_display_timezone(settings, monkeypatch):
    monkeypatch.setattr(settings, "tz_display", "Europe/Berlin")  # UTC+2 in October
    pref = notifications.NotificationPref(quiet_start=22 * 60, quiet_end=7 * 60)
    assert notifications.quiet_until(pref, datetime(2026, 10, 10, 20, 30)) == datetime(2026, 10, 11, 5, 0)
    assert notifications.quiet_until(pref, datetime(2026, 10, 10, 19, 30)) is None


def test_mute_all_and_reviews_separately():
    now = datetime(2026, 10, 10, 12, 0)
    pref = notifications.NotificationPref(mute_review_until=notifications.FOREVER)
    assert notifications.is_muted(pref, "review", now) and not notifications.is_muted(pref, "question", now)
    pref = notifications.NotificationPref(mute_all_until=now + timedelta(hours=1))
    assert notifications.is_muted(pref, "question", now)
    assert not notifications.is_muted(pref, "question", now + timedelta(hours=2))  # Expired by itself


def test_api_errors_are_mapped_and_hide_the_token(respx_mock):
    api = telegram.TelegramApi(TOKEN)
    respx_mock.post(f"{telegram.API_BASE}/bot{TOKEN}/sendMessage").mock(return_value=httpx.Response(
        429, json={"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 7}}))
    with pytest.raises(telegram.TelegramError) as e:
        api.send_message(1, "x")
    assert e.value.retry_after == 7 and not e.value.permanent
    respx_mock.post(f"{telegram.API_BASE}/bot{TOKEN}/getMe").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(telegram.TelegramError) as e:
        api.get_me()
    assert TOKEN not in str(e.value) and "ConnectError" in str(e.value)
    respx_mock.post(f"{telegram.API_BASE}/bot{TOKEN}/getUpdates").mock(return_value=httpx.Response(
        403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"}))
    with pytest.raises(telegram.TelegramError) as e:
        api.get_updates(None, 1)
    assert e.value.permanent


def test_poller_never_raises_and_backs_off(monkeypatch):
    from app.db import db as database

    thread = telegram_bot.BotThread()
    monkeypatch.setattr(thread, "acquire_lock", lambda: True)
    monkeypatch.setattr(type(database), "ready", property(lambda self: True))

    def boom(*a, **k):
        raise RuntimeError("database gone")

    monkeypatch.setattr(database, "session", boom)
    assert thread.step() == 5.0
    assert thread.step() == 10.0  # Exponential back-off, no exception
    assert telegram_bot.BotThread.MAX_BACKOFF_S == 300.0


def test_only_one_poller_holds_the_lock():
    first, second = telegram_bot.BotThread(), telegram_bot.BotThread()
    try:
        assert first.acquire_lock() and first.owns_lock
        assert not second.acquire_lock() and not second.owns_lock
        first.release_lock()
        assert second.acquire_lock()
    finally:
        first.release_lock()
        second.release_lock()


def test_unlinked_chat_is_ignored_without_database_access():
    class NoSession:
        def scalar(self, *a):
            return None

    assert telegram_bot.handle_update(NoSession(), start(5, "/status")) is None
    assert telegram_bot.handle_update(NoSession(), start(5, "hallo")) is None
    assert telegram_bot.handle_update(NoSession(), {"update_id": 3}) is None


# --- With the test database -----------------------------------------------------------------------

@pytest.fixture
def tenv(env, fake):  # noqa: F811
    """Bot enabled; project KET (cloud_ok, owner admin), OWN (local_only, owner alex)."""
    c = env["c"]
    mkproject(env)
    r = c.post("/api/v1/admin/actors", headers=env["admin"], json={
        "name": "alex", "display_name": "Alex", "actor_type": "human", "role": "member"})
    assert r.status_code == 201, r.text
    mkproject(env, key="OWN", privacy="local_only", owner="alex")
    env["alex"] = bearer(make_token("alex"))
    with db.session() as s:
        settings_store.update(s, {"telegram_enabled": "true", "telegram_bot_token": TOKEN}, None)
        s.commit()
    env["fake"] = fake
    return env


def link(name: str, chat_id: int) -> None:
    with db.session() as s:
        actor = s.scalar(select(Actor).where(Actor.name == name))
        code, _exp = telegram_bot.create_code(s, actor)
        assert telegram_bot.handle_update(s, start(chat_id, f"/start {code}")).startswith("Dieser Chat ist jetzt")
        s.commit()


def rows(**where):
    with db.session() as s:
        q = select(Notification).order_by(Notification.id)
        for k, v in where.items():
            q = q.where(getattr(Notification, k) == v)
        return list(s.scalars(q))


def deliver(now=None):
    with db.session() as s:
        counts = notifications.deliver(s, now=now)
        s.commit()
        return counts


def ask(env, nr, who="claude", text="Welche Datenbank?"):  # noqa: F811
    r = env["c"].post(f"/api/v1/tasks/{nr}/ask", headers=env[who], json={"body": text})
    assert r.status_code == 200, r.text


@pytest.mark.db
def test_bot_token_saved_encrypted_and_tested(env, ui, fake):  # noqa: F811
    r = post(ui, "/admin/settings", telegram_bot_token=TOKEN, telegram_enabled="1", merge_style="squash",
             ui_language="de", default_lease_minutes="120", join_code_minutes="15")
    assert "Gespeichert." in r.text
    with db.session() as s:
        stored = s.get(AppSetting, "telegram_bot_token")
        assert stored.is_secret and TOKEN not in stored.setting_value
        assert settings_store.get(s, "telegram_bot_token") == TOKEN and telegram.enabled(s)
    page = ui.get("/admin/settings").text
    assert TOKEN not in page and "Telegram-Verbindung testen" in page
    r = ui.post("/admin/settings/test-telegram", headers={"X-CSRF-Token": ui.csrf})
    assert "Telegram-Bot @tb_test_bot erreichbar." in r.text and fake.tokens == [TOKEN]
    fake.get_me = lambda: (_ for _ in ()).throw(telegram.TelegramError("Telegram 401: Unauthorized", 401))
    r = ui.post("/admin/settings/test-telegram", headers={"X-CSRF-Token": ui.csrf})
    assert "Telegram 401: Unauthorized" in r.text


@pytest.mark.db
def test_link_with_one_time_code_and_unlink(tenv, ui):  # noqa: F811
    page = ui.get("/profile").text
    assert "Nicht verknüpft" in page
    r = post(ui, "/profile/telegram/code")
    code = r.text.split("/start ")[1].split("<")[0].strip()
    assert "t.me/tb_test_bot?start=" in r.text or "Gültig bis" in r.text
    with db.session() as s:
        stored = s.scalars(select(TelegramLinkCode)).all()
        assert len(stored) == 1 and stored[0].code_hash == telegram_bot.hash_code(code) and code not in stored[0].code_hash
        assert telegram_bot.handle_update(s, start(777, f"/start {code}")).startswith("Dieser Chat ist jetzt mit")
        s.commit()
        # One-time: the same code does not link a second chat.
        assert "unbekannt, verbraucht oder abgelaufen" in telegram_bot.handle_update(s, start(888, f"/start {code}"))
        assert "/status" in telegram_bot.handle_update(s, start(777, "/help"))
        assert telegram_bot.handle_update(s, start(888, "/status")) is None  # Not linked: ignored
    page = ui.get("/profile").text
    assert "Verknüpft mit dem Chat alex_tg" in page
    r = post(ui, "/profile/telegram/unlink")
    assert "Telegram-Chat getrennt." in r.text
    with db.session() as s:
        assert s.get(TelegramLink, s.scalar(select(Actor.id).where(Actor.name == "admin"))) is None
    # /stop in the bot unlinks as well.
    link("admin", 777)
    with db.session() as s:
        assert "nicht mehr verknüpft" in telegram_bot.handle_update(s, start(777, "/stop"))
        s.commit()
        assert s.scalar(select(TelegramLink)) is None


@pytest.mark.db
def test_expired_code_is_refused(tenv):
    with db.session() as s:
        actor = s.scalar(select(Actor).where(Actor.name == "alex"))
        code, _exp = telegram_bot.create_code(s, actor)
        s.scalar(select(TelegramLinkCode)).expires_at = utcnow() - timedelta(seconds=1)
        s.flush()
        assert telegram_bot.redeem(s, code, 1, None) is None


@pytest.mark.db
def test_events_reach_the_right_human_and_local_only_hides_content(tenv):
    link("admin", 100)
    link("alex", 200)
    ket = mktask(tenv, assigned_to="claude-code", title="Login bauen")["task_nr"]
    claim(tenv, task_nr=ket)
    ask(tenv, ket)
    own = mktask(tenv, project="OWN", assigned_to="hermes", title="Geheimes Projekt")["task_nr"]
    claim(tenv, who="hermes", task_nr=own)
    ask(tenv, own, who="hermes", text="Geheime Frage")
    assert deliver()["sent"] == 2
    sent = dict(tenv["fake"].sent)
    assert set(sent) == {100, 200}  # Owner of each project only
    assert "Rückfrage eines Agenten" in sent[100] and "Login bauen" in sent[100] and "Welche Datenbank?" in sent[100]
    assert "Agent: claude-code" in sent[100] and f"https://tm.example/tasks/{ket}" in sent[100]
    assert sent[200].startswith(f"{own} · Rückfrage eines Agenten\nhttps://tm.example/tasks/{own}\n\n↩ ")  # local_only
    assert "Geheim" not in sent[200]
    # The delivery log keeps no content.
    assert all(n.status == "sent" and n.text is None for n in rows())


@pytest.mark.db
def test_admin_can_follow_all_projects(tenv):
    link("admin", 100)
    link("alex", 200)
    own = mktask(tenv, project="OWN", assigned_to="hermes")["task_nr"]
    claim(tenv, who="hermes", task_nr=own)
    ask(tenv, own, who="hermes")
    assert [n.actor_id for n in rows()] and len(rows()) == 1  # alex only
    with db.session() as s:
        admin = s.scalar(select(Actor).where(Actor.name == "admin"))
        notifications.save_prefs(s, admin, all_projects=True)
        alex = s.scalar(select(Actor).where(Actor.name == "alex"))
        notifications.save_prefs(s, alex, all_projects=True)  # Not an admin: ignored
        s.commit()
        assert not notifications.prefs(s, alex.id).all_projects
    tenv["c"].post(f"/api/v1/tasks/{own}/answer", headers=tenv["alex"], json={"body": "Antwort"})
    claim(tenv, who="hermes", task_nr=own)
    ask(tenv, own, who="hermes")
    assert len(rows()) == 3


@pytest.mark.db
def test_unlinked_owner_or_disabled_bot_gets_nothing(tenv):
    nr = mktask(tenv, assigned_to="claude-code")["task_nr"]
    claim(tenv, task_nr=nr)
    ask(tenv, nr)
    assert rows() == []  # admin not linked
    link("admin", 100)
    with db.session() as s:
        settings_store.update(s, {"telegram_enabled": "false"}, None)
        s.commit()
    tenv["c"].post(f"/api/v1/tasks/{nr}/answer", headers=tenv["admin"], json={"body": "ok"})
    claim(tenv, task_nr=nr)
    ask(tenv, nr)
    assert rows() == []


@pytest.mark.db
def test_event_selection_mute_and_quiet_hours(tenv, settings, monkeypatch):
    monkeypatch.setattr(settings, "tz_display", "UTC")
    link("admin", 100)
    with db.session() as s:
        admin = s.scalar(select(Actor).where(Actor.name == "admin"))
        notifications.save_prefs(s, admin, events={"review"}, mute_review=notifications.FOREVER)
        s.commit()
    nr = mktask(tenv, assigned_to="claude-code")["task_nr"]
    branch = claim(tenv, task_nr=nr).json()["branch_name"]
    ask(tenv, nr)
    assert rows() == []  # Event "question" switched off
    tenv["c"].post(f"/api/v1/tasks/{nr}/answer", headers=tenv["admin"], json={"body": "ok"})
    claim(tenv, task_nr=nr)
    tenv["gitea"].push_branch(SERVICE_USER, "ket", branch)
    r = tenv["c"].post(f"/api/v1/tasks/{nr}/submit", headers=tenv["claude"],
                       json={"branch_name": branch, "commit_sha": SHA, "summary": "Fertig mit Tests"})
    assert r.status_code == 200, r.text
    assert [(n.event, n.status, n.last_error) for n in rows()] == [("review", "skipped", "muted")]
    # Review mute off, quiet hours around now: held, then sent as one collected message afterwards.
    now = utcnow()
    with db.session() as s:
        admin = s.scalar(select(Actor).where(Actor.name == "admin"))
        start_min = (now.hour * 60 + now.minute - 30) % 1440
        notifications.save_prefs(s, admin, events=set(notifications.EVENTS), mute_review=None,
                                 quiet=(start_min, (start_min + 60) % 1440))
        notifications.task_event(s, _task(s, nr), "review", "eins")
        notifications.task_event(s, _task(s, nr), "question", "zwei")
        s.commit()
    held = rows(status="held")
    assert len(held) == 2 and held[0].next_attempt_at > now
    assert deliver()["sent"] == 0 and tenv["fake"].sent == []
    assert deliver(now=held[0].next_attempt_at)["sent"] == 2
    (chat, text), = tenv["fake"].sent
    assert chat == 100 and text.startswith("Gesammelt während der Ruhezeit (2):") and "eins" in text and "zwei" in text
    # Mute all until a time; /unmute in the bot lifts it.
    with db.session() as s:
        assert "stummgeschaltet" in telegram_bot.handle_update(s, start(100, "/mute 2h"))
        notifications.task_event(s, _task(s, nr), "question", "drei")
        assert "wieder eingeschaltet" in telegram_bot.handle_update(s, start(100, "/unmute"))
        assert "Unbekannte Dauer" in telegram_bot.handle_update(s, start(100, "/mute bald"))
        s.commit()
    assert rows()[-1].status == "skipped"


def _task(s, nr):
    from app.models import Task

    return s.scalar(select(Task).where(Task.task_nr == nr))


@pytest.mark.db
def test_profile_settings_form(tenv, ui, settings, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "tz_display", "UTC")
    r = post(ui, "/profile/notifications", ch_question_telegram="1", ch_review_telegram="1", quiet_start="22:00",
             quiet_end="07:00", mute_all="1d", mute_review="forever", all_projects="1")
    assert "Gespeichert." in r.text and 'value="22:00"' in r.text
    with db.session() as s:
        pref = notifications.prefs(s, s.scalar(select(Actor.id).where(Actor.name == "admin")))
        assert notifications.enabled_events(pref) == {"question", "review"}
        assert (pref.quiet_start, pref.quiet_end) == (22 * 60, 7 * 60) and pref.all_projects
        assert pref.mute_review_until == notifications.FOREVER
        assert timedelta(hours=23) < pref.mute_all_until - utcnow() <= timedelta(days=1)
    r = post(ui, "/profile/notifications", quiet_start="22:00", mute_all="until", mute_all_at="2020-01-01T10:00")
    assert r.status_code == 422 or "Zukunft" in r.text
    r = post(ui, "/profile/notifications", quiet_start="25:00", quiet_end="07:00")
    assert "Ungültige Uhrzeit" in r.text
    r = post(ui, "/profile/notifications", mute_all="off", mute_review="keep")
    with db.session() as s:
        pref = notifications.prefs(s, s.scalar(select(Actor.id).where(Actor.name == "admin")))
        assert pref.mute_all_until is None and pref.mute_review_until == notifications.FOREVER
        assert pref.quiet_start is None and notifications.enabled_events(pref) == set()


@pytest.mark.db
def test_profile_combined_form_saves_notifications_but_not_display_switches(tenv, ui, settings, monkeypatch):  # noqa: F811
    """'Save all changes' stores the notification settings in one POST to /profile/notifications; the display
    switches have their own form (`/profile/display`) and are ignored here."""
    monkeypatch.setattr(settings, "tz_display", "UTC")
    r = post(ui, "/profile/notifications", show_all_projects="1", show_all_agents="1",
             ch_question_telegram="1", ch_review_email="1", bundle="60", quiet_start="21:00", quiet_end="07:00",
             mute_all="forever", mute_review="off", all_projects="1")
    assert "Gespeichert." in r.text
    with db.session() as s:
        actor = s.scalar(select(Actor).where(Actor.name == "admin"))
        pref = notifications.prefs(s, actor.id)
        assert not actor.show_all_projects and not actor.show_all_agents
        assert notifications.channels_for(pref, "question") == {"telegram"}
        assert notifications.channels_for(pref, "review") == {"email"}
        assert notifications.bundle_minutes(pref) == 60
        assert (pref.quiet_start, pref.quiet_end) == (21 * 60, 7 * 60)
        assert pref.mute_all_until == notifications.FOREVER and pref.mute_review_until is None
        assert pref.all_projects


@pytest.mark.db
def test_channel_buttons_are_not_the_implicit_default(tenv, ui):  # noqa: F811
    """Telegram/email actions live in their own forms, not as formaction buttons in the settings form –
    so pressing Enter in a text/time field submits 'Save all changes' instead of unlinking the chat."""
    page = ui.get("/profile").text
    assert '<form id="pf-settings" method="post" action="/profile/notifications"' in page
    assert '<form method="post" action="/profile/telegram/code">' in page
    assert '<form method="post" action="/profile/email">' in page
    assert 'formaction="/profile/telegram/' not in page
    # The settings form carries no submit button other than "Save all changes" (nothing before the actions block).
    form = page.split('<form id="pf-settings"', 1)[1].split('<div class="pf-actions">', 1)[0]
    assert "<button" not in form


@pytest.mark.db
def test_delivery_retries_and_survives_telegram_outage(tenv):
    link("admin", 100)
    nr = mktask(tenv, assigned_to="claude-code")["task_nr"]
    claim(tenv, task_nr=nr)
    tenv["fake"].fail = telegram.TelegramError("Telegram nicht erreichbar (ConnectError)")
    ask(tenv, nr)  # The workflow is not affected by Telegram being down.
    assert tenv["c"].get(f"/api/v1/tasks/{nr}", headers=tenv["admin"]).json()["status"] == "questions"
    assert deliver()["retry"] == 1
    (n,) = rows()
    assert n.status == "queued" and n.attempts == 1 and n.text and n.next_attempt_at > utcnow()
    assert deliver()["retry"] == 0  # Not due yet
    tenv["fake"].fail = telegram.TelegramError("Too Many Requests", 429, retry_after=30)
    assert deliver(now=n.next_attempt_at)["retry"] == 1
    tenv["fake"].fail = None
    assert deliver(now=utcnow() + timedelta(hours=1))["sent"] == 1
    (n,) = rows()
    assert n.status == "sent" and n.text is None and n.sent_at is not None
    # Permanent error (bot blocked by the user): given up without retries, content dropped.
    tenv["c"].post(f"/api/v1/tasks/{nr}/answer", headers=tenv["admin"], json={"body": "ok"})
    claim(tenv, task_nr=nr)
    ask(tenv, nr)
    tenv["fake"].fail = telegram.TelegramError("Telegram 403: Forbidden", 403)
    assert deliver()["failed"] == 1
    assert rows()[-1].status == "failed" and rows()[-1].text is None and "403" in rows()[-1].last_error


@pytest.mark.db
def test_max_attempts_then_failed(tenv):
    link("admin", 100)
    nr = mktask(tenv, assigned_to="claude-code")["task_nr"]
    claim(tenv, task_nr=nr)
    ask(tenv, nr)
    tenv["fake"].fail = telegram.TelegramError("Telegram 502: Bad Gateway", 502)
    when = utcnow()
    for _attempt in range(notifications.MAX_ATTEMPTS):
        when += timedelta(hours=2)
        deliver(now=when)
    (n,) = rows()
    assert n.status == "failed" and n.attempts == notifications.MAX_ATTEMPTS and n.text is None


@pytest.mark.db
def test_more_events_abort_lease_conflict_runner_signals(tenv):
    link("admin", 100)
    c, g = tenv["c"], tenv["gitea"]
    a = mktask(tenv, assigned_to="claude-code")["task_nr"]
    claim(tenv, task_nr=a)
    c.post(f"/api/v1/tasks/{a}/abort", headers=tenv["claude"], json={"reason": "Modell zu klein"})
    b = mktask(tenv, assigned_to="claude-code")["task_nr"]
    claim(tenv, task_nr=b)
    c.post(f"/api/v1/tasks/{b}/comments", headers=tenv["claude"],
           json={"body": "Keine Aktivität seit 10:00\n```\nlast lines\n```", "signal": "stalled"})
    c.post(f"/api/v1/tasks/{b}/comments", headers=tenv["claude"], json={"body": "normaler Kommentar"})
    c.post(f"/api/v1/tasks/{b}/release", headers=tenv["claude"], json={"reason": "später weiter"})
    claim(tenv, task_nr=b)
    c.post(f"/api/v1/tasks/{b}/release", headers=tenv["claude"],
           json={"reason": "claude-code: Login abgelaufen", "signal": "blocked"})
    claim(tenv, task_nr=b)
    with db.session() as s:
        t = _task(s, b)
        t.lease_expires_at = utcnow() - timedelta(minutes=1)
        s.commit()
        from app.services.tasks import sweep_expired_leases

        assert sweep_expired_leases(s) == [b]
        s.commit()
    # Merge conflict: the approval fails (409, rollback), the notification stays.
    branch = claim(tenv, task_nr=b).json()["branch_name"]
    g.push_branch(SERVICE_USER, "ket", branch)
    c.post(f"/api/v1/tasks/{b}/submit", headers=tenv["claude"], json={"branch_name": branch, "commit_sha": SHA,
                                                                      "summary": "x"})
    g.merge_fails = True
    assert c.post(f"/api/v1/tasks/{b}/review", headers=tenv["admin"], json={"decision": "approve"}).status_code == 409
    events = [n.event for n in rows()]
    assert events == ["aborted", "agent_blocked", "agent_blocked", "lease_expired", "review", "conflict"]
    deliver()
    texts = [t for _chat, t in tenv["fake"].sent]
    assert "Modell zu klein" in texts[0] and "Keine Aktivität seit 10:00" in texts[1] and "last lines" not in texts[1]
    assert "Login abgelaufen" in texts[2] and "Fertig" not in texts[4] and "Merge" in texts[5]


@pytest.mark.db
def test_sprint_completed_once(tenv):
    link("admin", 100)
    c = tenv["c"]
    s = c.post("/api/v1/projects/KET/sprints", headers=tenv["admin"], json={"name": "Sprint 1"}).json()
    one = mktask(tenv, sprint=s["id"])["task_nr"]
    two = mktask(tenv, sprint=s["id"])["task_nr"]
    c.post(f"/api/v1/projects/KET/sprints/{s['id']}/release", headers=tenv["admin"], json={})
    c.post(f"/api/v1/tasks/{one}/transition", headers=tenv["admin"], json={"to": "cancelled"})
    assert rows() == []  # One task still open
    r = c.post(f"/api/v1/tasks/{two}/transition", headers=tenv["admin"], json={"to": "done", "override": True})
    assert r.status_code == 200, r.text
    (n,) = rows()
    assert n.event == "sprint_done" and "Sprint 1" in n.text and f"/projects/KET/sprints/{s['id']}" in n.text
