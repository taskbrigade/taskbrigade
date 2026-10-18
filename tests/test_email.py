"""Email notifications: SMTP client, message headers, address verification, channel choice, delivery with retry,
throttle and bundling. SMTP goes to `tests/smtp_fake.FakeSmtp` on 127.0.0.1; no test sends real mail."""
import fcntl
import os
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db import db
from app.errors import NotFound, ProbeFailed
from app.models import Actor, AppSetting, EmailVerifyCode, Notification, Task
from app.services import mail, notifications, settings_store
from app.services.actors import utcnow
from tests.smtp_fake import FakeSmtp, client_context
from tests.test_telegram import TOKEN, FakeApi, link  # noqa: F401 (FakeApi via fixture)
from tests.test_ui_cli import post, ui  # noqa: F401 (Fixtures)
from tests.test_workflow import claim, env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)

USER, PASSWORD = "taskbrigade", "app-password-123"


@pytest.fixture
def smtp(monkeypatch):
    server = FakeSmtp()
    monkeypatch.setattr(mail, "ssl_context_factory", client_context)
    yield server
    server.stop()


def _cfg(server: FakeSmtp, **over) -> mail.Config:
    values = {"host": "127.0.0.1", "port": server.port, "security": server.security, "user": USER,
              "password": PASSWORD, "from_address": "taskbrigade@example.org", "from_name": "TaskBrigade",
              "rate_per_hour": 60}
    return mail.Config(**{**values, **over})


class _NoSettings:
    """Session stand-in for message building without a database: every setting falls back to its default."""

    def get(self, *_args):
        return None


def _msg(cfg, to="alex@example.org", name="Alex", subject="Hi", text="Text"):
    return mail.build_message(_NoSettings(), cfg, to, name, subject, text, base_url="https://tm.example")


# --- Without a database ---------------------------------------------------------------------------

def test_addresses_and_header_injection():
    assert mail.check_address(" alex@example.org ") == "alex@example.org"
    for bad in ("alex", "alex@", "a b@example.org", "alex@example.org\r\nBcc: x@example.org",
                "Alex <alex@example.org>", "alex@example.org,x@example.org"):
        with pytest.raises(ValueError):
            mail.check_address(bad)
    cfg = mail.Config("h", 587, "starttls", None, None, "taskbrigade@example.org", "Task\r\nBcc: x@example.org", 60)
    msg = _msg(cfg, name="Alex\nBcc: y@example.org", subject="KET-0001 · Review\r\nBcc: z@example.org")
    raw = msg.as_bytes()
    assert b"\nBcc:" not in raw and msg["Subject"] == "KET-0001 · Review Bcc: z@example.org"
    assert msg["Auto-Submitted"] == "auto-generated" and msg["List-Unsubscribe"] == \
        "<https://tm.example/profile#notifications>"
    assert msg["Message-ID"].endswith("@tm.example>")
    with pytest.raises(ValueError):
        _msg(cfg, to="x@example.org\nBcc: y@example.org")


def test_message_is_text_plus_simple_html_without_tracking():
    cfg = mail.Config("h", 587, "starttls", None, None, "taskbrigade@example.org", "TaskBrigade", 60)
    msg = _msg(cfg, text="KET-0001 · Review\n<b>Login</b> & mehr\nhttps://tm.example/tasks/KET-0001")
    text = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    assert "Antworten auf diese Mail werden nicht verarbeitet" in text or "Replies to this mail" in text
    assert "https://tm.example/profile#notifications" in text
    assert "&lt;b&gt;Login&lt;/b&gt; &amp; mehr" in html and "<b>" not in html
    assert '<a href="https://tm.example/tasks/KET-0001">' in html
    assert "<img" not in html and "src=" not in html


def test_starttls_and_tls_with_login(smtp):
    conn = mail.Connection(_cfg(smtp))
    conn.send(_msg(_cfg(smtp)), "alex@example.org")
    conn.close()
    assert smtp.messages[0][:2] == ("taskbrigade@example.org", ["alex@example.org"])
    tls = FakeSmtp(security="tls")
    try:
        conn = mail.Connection(_cfg(tls))
        conn.send(_msg(_cfg(tls)), "alex@example.org")
        conn.close()
        assert len(tls.messages) == 1
    finally:
        tls.stop()


def test_smtp_errors_are_plain_and_classified(smtp):
    with pytest.raises(mail.MailError) as e:
        mail.Connection(_cfg(smtp, password="wrong"))
    assert "535" in str(e.value) and "wrong" not in str(e.value) and not e.value.permanent
    conn = mail.Connection(_cfg(smtp))
    smtp.fail_rcpt = (550, "5.1.1 No such user")
    with pytest.raises(mail.MailError) as e:
        conn.send(_msg(_cfg(smtp)), "alex@example.org")
    assert e.value.permanent and "550" in str(e.value) and "No such user" in str(e.value)
    smtp.fail_rcpt = (451, "4.3.0 Try again later")
    with pytest.raises(mail.MailError) as e:
        conn.send(_msg(_cfg(smtp)), "alex@example.org")
    assert not e.value.permanent and not e.value.connection
    smtp.fail_rcpt, smtp.fail_data = None, (554, "5.6.0 Message rejected")
    with pytest.raises(mail.MailError) as e:
        conn.send(_msg(_cfg(smtp)), "alex@example.org")
    assert e.value.permanent
    conn.close()
    port = smtp.port
    smtp.stop()
    with pytest.raises(mail.MailError) as e:
        mail.Connection(_cfg(smtp, port=port))
    assert e.value.connection and "ConnectionRefusedError" in str(e.value)


def test_untrusted_certificate_is_refused(smtp, monkeypatch):
    monkeypatch.setattr(mail, "ssl_context_factory", __import__("ssl").create_default_context)
    with pytest.raises(mail.MailError) as e:
        mail.Connection(_cfg(smtp))
    assert e.value.connection and smtp.messages == []


def test_only_one_process_sends(state_dir):
    mail.release_lock()
    assert mail.owns_lock() and mail.owns_lock()
    fd = os.open(Path(state_dir) / mail.LOCK_FILE, os.O_RDWR)
    try:
        with pytest.raises(OSError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
        mail.release_lock()


def test_no_code_path_reads_mail():
    root = Path(__file__).resolve().parent.parent
    for path in [*root.glob("app/**/*.py"), *root.glob("cli/**/*.py")]:
        source = path.read_text()
        assert not re.search(r"\b(imaplib|poplib|IMAP4|POP3)\b", source), path


def test_bundling_groups():
    now = utcnow()
    row = lambda status="queued", age=0, attempts=0: SimpleNamespace(  # noqa: E731
        status=status, created_at=now - timedelta(minutes=age), attempts=attempts)
    fresh, old = row(age=1), row(age=20)
    assert mail._groups([fresh, old], 0, now) == [[fresh], [old]]
    held = [row("held"), row("held")]
    assert mail._groups(held + [fresh], 0, now) == [held, [fresh]]
    assert mail._groups([fresh], 15, now) == []
    assert mail._groups([fresh, old], 15, now) == [[fresh, old]]
    assert mail._groups([row(attempts=1)], 15, now) != []  # A retry does not wait for the window again


# --- With the test database -----------------------------------------------------------------------

@pytest.fixture
def menv(env, smtp, monkeypatch):  # noqa: F811
    """Email on (fake SMTP); project KET (cloud_ok, owner admin), OWN (local_only, owner alex)."""
    monkeypatch.setattr("app.services.telegram.api_factory", FakeApi())
    c = env["c"]
    mkproject(env)
    r = c.post("/api/v1/admin/actors", headers=env["admin"], json={
        "name": "alex", "display_name": "Alex", "actor_type": "human", "role": "member"})
    assert r.status_code == 201, r.text
    mkproject(env, key="OWN", privacy="local_only", owner="alex")
    with db.session() as s:
        settings_store.update(s, {"email_enabled": "true", "smtp_host": "127.0.0.1", "smtp_port": str(smtp.port),
                                  "smtp_security": "starttls", "smtp_user": USER, "smtp_password": PASSWORD,
                                  "mail_from_address": "taskbrigade@example.org"}, None)
        s.commit()
    env["smtp"] = smtp
    return env


def verified(name: str, address: str) -> None:
    with db.session() as s:
        actor = s.scalar(select(Actor).where(Actor.name == name))
        actor.email, actor.email_verified_at = address, utcnow()
        s.commit()


def task(s, nr):
    return s.scalar(select(Task).where(Task.task_nr == nr))


def rows(**where):
    with db.session() as s:
        q = select(Notification).order_by(Notification.id)
        for k, v in where.items():
            q = q.where(getattr(Notification, k) == v)
        return list(s.scalars(q))


def deliver(now=None):
    with db.session() as s:
        counts = mail.deliver(s, now=now)
        s.commit()
        return counts


def event(nr, name="review", content="Fertig mit Tests"):
    with db.session() as s:
        notifications.task_event(s, task(s, nr), name, content)
        s.commit()


@pytest.mark.db
def test_settings_password_secret_and_test_mail(menv, ui):  # noqa: F811
    with db.session() as s:
        stored = s.get(AppSetting, "smtp_password")
        assert stored.is_secret and PASSWORD not in stored.setting_value
    page = ui.get("/admin/settings").text
    assert PASSWORD not in page and "Testmail senden" in page and 'id="settings-email"' in page
    r = ui.post("/admin/settings/test-email", data={"to": "admin@example.org"}, headers={"X-CSRF-Token": ui.csrf})
    assert "Testmail an admin@example.org verschickt." in r.text
    msg = menv["smtp"].parsed()
    assert msg["To"] == "admin@example.org" and msg["Subject"] == "[TaskBrigade] Testmail"
    r = ui.post("/admin/settings/test-email", data={"to": "x@example.org\r\nBcc: y@example.org"},
                headers={"X-CSRF-Token": ui.csrf})
    assert "Ungültige E-Mail-Adresse" in r.text and len(menv["smtp"].messages) == 1
    with db.session() as s:
        settings_store.update(s, {"smtp_password": "wrong-password"}, None)
        s.commit()
    r = ui.post("/admin/settings/test-email", data={"to": "admin@example.org"}, headers={"X-CSRF-Token": ui.csrf})
    assert "SMTP 535" in r.text and "wrong-password" not in r.text
    r = post(ui, "/admin/settings", mail_from_address="kein-absender", merge_style="squash", ui_language="de",
             default_lease_minutes="120", join_code_minutes="15", smtp_security="starttls")
    assert "erwartet eine E-Mail-Adresse" in r.text


@pytest.mark.db
def test_address_verification_with_one_time_link(menv, ui):  # noqa: F811
    nr = mktask(menv)["task_nr"]
    r = post(ui, "/profile/email", email="admin@example.org")
    assert "Bestätigungslink verschickt" in r.text
    msg = menv["smtp"].parsed()
    assert msg["To"].addresses[0].addr_spec == "admin@example.org" and "bestätigen" in msg["Subject"]
    code = re.search(r"/profile/email/verify\?code=([\w-]+)", msg.get_body(("plain",)).get_content()).group(1)
    with db.session() as s:
        stored = s.scalar(select(EmailVerifyCode))
        assert stored.code_hash == mail.hash_code(code) and code not in stored.code_hash
        assert stored.expires_at - utcnow() > timedelta(hours=23)
    event(nr)
    assert rows(channel="email") == []  # Not confirmed yet: no mail
    r = ui.get(f"/profile/email/verify?code={code}")
    assert "E-Mail-Adresse bestätigt" in r.text
    assert ui.get(f"/profile/email/verify?code={code}").status_code == 410  # One-time
    event(nr)
    assert len(rows(channel="email")) == 1
    # A new address is unconfirmed again, and an old link does not confirm it.
    post(ui, "/profile/email", email="other@example.org")
    with db.session() as s:
        admin = s.scalar(select(Actor).where(Actor.name == "admin"))
        assert admin.email == "other@example.org" and admin.email_verified_at is None
        actor = s.scalar(select(Actor).where(Actor.name == "admin"))
        old_code, _exp = mail.create_verify_code(s, actor)
        actor.email = "third@example.org"
        s.flush()
        assert mail.redeem(s, old_code) is None


@pytest.mark.db
def test_expired_link_is_refused(menv):
    verified("alex", "alex@example.org")
    with db.session() as s:
        actor = s.scalar(select(Actor).where(Actor.name == "alex"))
        actor.email_verified_at = None
        code, _exp = mail.create_verify_code(s, actor)
        s.scalar(select(EmailVerifyCode)).expires_at = utcnow() - timedelta(seconds=1)
        s.flush()
        assert mail.redeem(s, code) is None


@pytest.mark.db
def test_default_channels_mail_content_and_local_only(menv):
    verified("admin", "admin@example.org")
    verified("alex", "alex@example.org")
    ket = mktask(menv, assigned_to="claude-code", title="Login bauen")["task_nr"]
    own = mktask(menv, project="OWN", assigned_to="hermes", title="Geheimes Projekt")["task_nr"]
    event(ket, "question", "Welche Datenbank?")
    assert rows(channel="email") == []  # Default: questions only via Telegram
    event(ket)
    event(own, content="Geheimer Inhalt")
    assert deliver()["sent"] == 2
    by_to = {m["To"].addresses[0].addr_spec: m for m in (menv["smtp"].parsed(i) for i in range(2))}
    ket_mail, own_mail = by_to["admin@example.org"], by_to["alex@example.org"]
    assert ket_mail["Subject"] == f"[TaskBrigade] {ket} · Task zur Abnahme"
    body = ket_mail.get_body(("plain",)).get_content()
    assert "Login bauen" in body and "Fertig mit Tests" in body and f"https://tm.example/tasks/{ket}" in body
    assert "nicht verarbeitet" in body
    assert ket_mail["Auto-Submitted"] == "auto-generated" and ket_mail["Message-ID"].endswith("@tm.example>")
    assert ket_mail["List-Unsubscribe"] == "<https://tm.example/profile#notifications>"
    own_body = own_mail.get_body(("plain",)).get_content().replace("\r\n", "\n")
    assert own_body.startswith(f"{own} · Task zur Abnahme\nhttps://tm.example/tasks/{own}")
    assert "Geheim" not in own_body and "Geheim" not in own_mail.get_body(("html",)).get_content()
    assert "Geheim" not in own_mail["Subject"]
    # Log without content, with the Message-ID.
    assert all(n.status == "sent" and n.text is None and n.email_message_id for n in rows(channel="email"))


@pytest.mark.db
def test_channel_choice_per_event(menv):
    verified("admin", "admin@example.org")
    with db.session() as s:
        settings_store.update(s, {"telegram_enabled": "true", "telegram_bot_token": TOKEN}, None)
        s.commit()
    link("admin", 100)
    nr = mktask(menv)["task_nr"]
    with db.session() as s:
        admin = s.scalar(select(Actor).where(Actor.name == "admin"))
        notifications.save_prefs(s, admin, channels={"question": {"email"}, "review": {"telegram"},
                                                     "aborted": {"telegram", "email"}})
        s.commit()
        assert notifications.enabled_events(notifications.prefs(s, admin.id)) == {"question", "review", "aborted"}
    for name in ("question", "review", "aborted", "conflict"):
        event(nr, name)
    assert [(n.event, n.channel) for n in rows()] == [
        ("question", "email"), ("review", "telegram"), ("aborted", "email"), ("aborted", "telegram")]
    # Each job only sends its own channel.
    assert deliver()["sent"] == 2
    assert {n.status for n in rows(channel="telegram")} == {"queued"}


@pytest.mark.db
def test_profile_form_saves_channels_and_bundling(menv, ui):  # noqa: F811
    r = post(ui, "/profile/notifications", ch_review_email="1", ch_question_telegram="1", bundle="60",
             quiet_start="22:00", quiet_end="07:00", mute_all="keep", mute_review="keep")
    assert "Gespeichert." in r.text and 'name="ch_review_email" value="1" aria-label' in r.text
    with db.session() as s:
        pref = notifications.prefs(s, s.scalar(select(Actor.id).where(Actor.name == "admin")))
        assert notifications.channels_for(pref, "review") == {"email"}
        assert notifications.channels_for(pref, "question") == {"telegram"}
        assert notifications.channels_for(pref, "sprint_done") == set()
        assert notifications.bundle_minutes(pref) == 60
    r = post(ui, "/profile/notifications", bundle="7")
    assert "Unbekanntes Bündelintervall" in r.text


@pytest.mark.db
def test_retry_backoff_and_permanent_failure(menv):
    verified("admin", "admin@example.org")
    nr = mktask(menv)["task_nr"]
    event(nr)
    smtp = menv["smtp"]
    smtp.fail_rcpt = (451, "4.3.0 Try again later")
    assert deliver()["retry"] == 1
    (n,) = rows()
    assert n.status == "queued" and n.attempts == 1 and n.text and n.next_attempt_at > utcnow()
    assert "451" in n.last_error
    assert deliver()["retry"] == 0  # Not due yet
    smtp.fail_rcpt = None
    assert deliver(now=n.next_attempt_at)["sent"] == 1
    (n,) = rows()
    assert n.status == "sent" and n.text is None and n.sent_at is not None
    event(nr)
    smtp.fail_rcpt = (550, "5.1.1 Mailbox unavailable")
    assert deliver()["failed"] == 1
    last = rows()[-1]
    assert last.status == "failed" and last.text is None and "550" in last.last_error and last.attempts == 1


@pytest.mark.db
def test_server_down_then_max_attempts(menv):
    verified("admin", "admin@example.org")
    nr = mktask(menv)["task_nr"]
    event(nr)
    menv["smtp"].stop()
    when = utcnow()
    assert deliver(now=when)["retry"] == 1
    for _attempt in range(notifications.MAX_ATTEMPTS - 1):
        when += timedelta(hours=2)
        deliver(now=when)
    (n,) = rows()
    assert n.status == "failed" and n.attempts == notifications.MAX_ATTEMPTS and n.text is None
    assert "nicht erreichbar" in n.last_error


@pytest.mark.db
def test_throttle_below_rate_per_hour(menv):
    verified("admin", "admin@example.org")
    with db.session() as s:
        settings_store.update(s, {"mail_rate_per_hour": "2"}, None)
        s.commit()
    nr = mktask(menv)["task_nr"]
    for _i in range(3):
        event(nr)
    now = utcnow()
    counts = deliver(now=now)
    assert counts["sent"] == 2 and counts["throttled"] == 1 and len(menv["smtp"].messages) == 2
    assert deliver(now=now + timedelta(minutes=30))["sent"] == 0
    assert rows()[-1].status == "queued"
    assert deliver(now=now + timedelta(minutes=61))["sent"] == 1


@pytest.mark.db
def test_bundling_collects_mails(menv):
    verified("admin", "admin@example.org")
    with db.session() as s:
        notifications.save_prefs(s, s.scalar(select(Actor).where(Actor.name == "admin")), bundle=15)
        s.commit()
    nr = mktask(menv)["task_nr"]
    event(nr, content="eins")
    event(nr, "sprint_done", content=None)
    event(nr, content="zwei")
    now = utcnow()
    assert deliver(now=now)["sent"] == 0 and menv["smtp"].messages == []
    assert deliver(now=now + timedelta(minutes=16))["sent"] == 3
    (_from, rcpts, _raw), = menv["smtp"].messages
    msg = menv["smtp"].parsed()
    body = msg.get_body(("plain",)).get_content()
    assert rcpts == ["admin@example.org"] and msg["Subject"] == "[TaskBrigade] 3 Benachrichtigungen"
    assert body.startswith("Gesammelte Benachrichtigungen (3):") and "eins" in body and "zwei" in body
    assert len({n.email_message_id for n in rows()}) == 1


@pytest.mark.db
def test_mute_and_quiet_hours_apply_to_email(menv, settings, monkeypatch):
    monkeypatch.setattr(settings, "tz_display", "UTC")
    verified("admin", "admin@example.org")
    nr = mktask(menv)["task_nr"]
    with db.session() as s:
        admin = s.scalar(select(Actor).where(Actor.name == "admin"))
        notifications.save_prefs(s, admin, mute_all=notifications.FOREVER)
        s.commit()
    event(nr)
    assert [(n.channel, n.status, n.last_error) for n in rows()] == [("email", "skipped", "muted")]
    now = utcnow()
    with db.session() as s:
        admin = s.scalar(select(Actor).where(Actor.name == "admin"))
        start_min = (now.hour * 60 + now.minute - 30) % 1440
        notifications.save_prefs(s, admin, mute_all=None, quiet=(start_min, (start_min + 60) % 1440))
        s.commit()
    event(nr, content="eins")
    event(nr, content="zwei")
    held = rows(status="held")
    assert len(held) == 2 and held[0].next_attempt_at > now
    assert deliver()["sent"] == 0
    assert deliver(now=held[0].next_attempt_at)["sent"] == 2
    msg = menv["smtp"].parsed()
    assert msg.get_body(("plain",)).get_content().startswith("Gesammelt während der Ruhezeit (2):")


@pytest.mark.db
def test_channel_off_keeps_queue_and_admin_log_shows_channel(menv, ui):  # noqa: F811
    verified("admin", "admin@example.org")
    nr = mktask(menv)["task_nr"]
    event(nr)
    with db.session() as s:
        settings_store.update(s, {"email_enabled": "false"}, None)
        s.commit()
    assert deliver()["sent"] == 0 and rows()[0].status == "queued"
    event(nr)
    assert len(rows()) == 1  # Channel off: no new email rows
    page = ui.get("/admin/settings").text
    assert "<th>Kanal</th>" in page and "<td>E-Mail</td>" in page


# --- SMTP probe for tbr doctor ------------------------------------------------------------------------

def probe():
    with db.session() as s:
        return mail.smtp_probe(s)


@pytest.mark.db
def test_smtp_probe_ok_and_delivery_log(menv):
    verified("admin", "admin@example.org")
    nr = mktask(menv)["task_nr"]
    d = probe()
    assert d["ok"] is True and d["host"] == "127.0.0.1"
    assert menv["smtp"].messages == []  # A probe sends no mail
    assert d["last_delivery"]["sent_at"] is None and d["last_delivery"]["last_status"] is None
    event(nr)
    assert deliver()["sent"] == 1
    d = probe()
    assert d["last_delivery"]["sent_at"] and d["last_delivery"]["last_status"] == "sent"
    assert d["last_delivery"]["last_error"] is None and d["failures_in_a_row"] == 0


@pytest.mark.db
def test_smtp_probe_counts_failures_in_a_row(menv):
    verified("admin", "admin@example.org")
    nr = mktask(menv)["task_nr"]
    event(nr)
    with db.session() as s:  # A delivery that keeps failing: wrong password, back-off keeps the row queued
        settings_store.update(s, {"smtp_password": "wrong-password"}, None)
        s.commit()
    counts = deliver()
    assert counts["sent"] == 0 and counts["retry"] == 1
    assert rows()[0].attempts == 1 and rows()[0].last_error
    with pytest.raises(ProbeFailed):
        probe()  # The probe logs in with the stored (wrong) password – the endpoint answers 502 bad_gateway
    with db.session() as s:  # Settings fixed → the probe clears the picture
        settings_store.update(s, {"smtp_password": PASSWORD}, None)
        s.commit()
    d = probe()
    assert d["ok"] is True and d["failures_in_a_row"] == 1


@pytest.mark.db
def test_smtp_probe_refuses_without_settings(menv):
    verified("admin", "admin@example.org")
    del menv["smtp"]
    with db.session() as s:
        settings_store.update(s, {"smtp_host": None}, None)
        s.commit()
    with pytest.raises(NotFound):
        probe()


@pytest.mark.db
def test_email_status_endpoint_for_doctor(menv):
    """`tbr doctor` asks the endpoint (no credentials in the answer); the channel off → 404 email_disabled."""
    verified("admin", "admin@example.org")
    r = menv["c"].get("/api/v1/agents/me/email-status", headers=menv["admin"])
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "app-password-123" not in r.text and PASSWORD not in r.text
    with db.session() as s:
        settings_store.update(s, {"email_enabled": "false"}, None)
        s.commit()
    r = menv["c"].get("/api/v1/agents/me/email-status", headers=menv["admin"])
    assert r.status_code == 404 and r.json()["error"]["code"] == "email_disabled"


@pytest.mark.db
def test_email_status_endpoint_probe_fails(menv):
    """Login refused → 502 `bad_gateway` with the plain SMTP reason; the password never appears in the answer."""
    verified("admin", "admin@example.org")
    with db.session() as s:
        settings_store.update(s, {"smtp_password": "wrong-password"}, None)
        s.commit()
    r = menv["c"].get("/api/v1/agents/me/email-status", headers=menv["admin"])
    assert r.status_code == 502, r.text
    body = r.json()["error"]
    # The SMTP code and the server's own reply come through in every UI language; the reason is plain text.
    assert body["code"] == "bad_gateway" and "535" in body["message"] and "Authentication credentials invalid" in body["message"]
    assert "wrong-password" not in r.text and PASSWORD not in r.text
