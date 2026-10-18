"""Inbox read actions: snapshot cutoff, sprint scope, actor isolation and exact undo."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db import db
from app.models import Actor, Notification, Task
from app.services import notifications
from tests.test_ui_screens import browser, server  # noqa: F401
from tests.test_ui_cli import ui  # noqa: F401
from tests.test_workflow import env, gitea, mkproject, mktask  # noqa: F401

pytestmark = pytest.mark.db


def seed(env):
    mkproject(env)
    sid = env['c'].post('/api/v1/projects/KET/sprints', headers=env['admin'], json={'name': 'Wave'}).json()['id']
    task = mktask(env, status='draft')['task_nr']
    other_task = mktask(env, status='draft')['task_nr']
    cutoff = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    with db.session() as s:
        owner = s.scalar(select(Actor).where(Actor.name == 'admin'))
        other = s.scalar(select(Actor).where(Actor.name == 'codex'))
        s.scalar(select(Task).where(Task.task_nr == task)).sprint_id = sid
        rows = [Notification(actor_id=owner.id, event='question', task_nr=task,
                             created_at=cutoff - timedelta(seconds=i + 1)) for i in range(3)]
        later = Notification(actor_id=owner.id, event='question', task_nr=task,
                             created_at=cutoff + timedelta(seconds=10))
        foreign = Notification(actor_id=other.id, event='question', task_nr=task, created_at=cutoff)
        outside = Notification(actor_id=owner.id, event='question', task_nr=other_task, created_at=cutoff)
        read = Notification(actor_id=owner.id, event='question', task_nr=task, created_at=cutoff,
                            read_at=cutoff)
        s.add_all([*rows, later, foreign, outside, read])
        s.flush()
        ids = [r.id for r in rows]
        extras = [r.id for r in (later, foreign, outside, read)]
        s.commit()
    return sid, cutoff.isoformat() + 'Z', ids, extras


def test_all_read_is_scoped_and_undo_is_exact(env, ui):  # noqa: F811
    sid, cutoff, ids, extras = seed(env)
    headers = {'X-CSRF-Token': ui.csrf}
    response = ui.post('/inbox/notices/read', json={'all': True, 'sprint': str(sid), 'until': cutoff}, headers=headers)
    assert response.status_code == 200, response.text
    assert set(response.json()['ids']) == set(ids)
    assert response.json()['count'] == 3
    with db.session() as s:
        assert all(s.get(Notification, i).read_at for i in ids)
        assert all(s.get(Notification, i).read_at is None for i in extras[:3])
        assert s.get(Notification, extras[3]).read_at is not None
    # The response IDs are the undo payload; the pre-existing read notice stays read.
    response = ui.post('/inbox/notices/unread', json={'ids': ids + [extras[1]]}, headers=headers)
    assert set(response.json()['ids']) == set(ids)
    with db.session() as s:
        assert all(s.get(Notification, i).read_at is None for i in ids)
        assert s.get(Notification, extras[3]).read_at is not None
        assert s.scalar(select(Notification).where(Notification.id == extras[1])) is not None


def test_selected_and_single_actions_only_change_own_unread_ids(env, ui):  # noqa: F811
    sid, cutoff, ids, extras = seed(env)
    headers = {'X-CSRF-Token': ui.csrf}
    assert ui.post('/inbox/notices/read', json={'ids': [ids[0], extras[1], extras[3]]}, headers=headers).json()['ids'] == ids[:1]
    response = ui.post('/inbox/notices/read', json={'ids': ids}, headers=headers)
    assert set(response.json()['ids']) == set(ids[1:])
    with db.session() as s:
        owner = s.scalar(select(Actor).where(Actor.name == 'admin'))
        assert not notifications.notices_for(s, owner, sprint=str(sid), until=datetime.fromisoformat(cutoff))
        older = notifications.notices_for(s, owner, sprint=str(sid), include_read=True)
        assert set(ids) <= {i for n in older for i in n['ids']}
        assert s.get(Notification, extras[1]).read_at is None
    # CSRF/origin checks protect JSON actions too; a cutoff is mandatory for bulk actions.
    assert ui.post('/inbox/notices/read', json={'ids': ids}).status_code == 403
    assert ui.post('/inbox/notices/unread', json={'ids': ids}, headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
    assert ui.post('/inbox/notices/read', json={'all': True}, headers=headers).status_code == 422
    assert ui.post('/inbox/notices/read', json={'all': True, 'until': cutoff, 'sprint': 'invalid'}, headers=headers).status_code == 422


def test_no_sprint_filter_matches_feed(env, ui):  # noqa: F811
    sid, cutoff, ids, extras = seed(env)
    html = ui.get('/?sprint=none').text
    assert f'data-notice-ids="{extras[2]}"' in html
    assert f'data-notice-ids="{ids[0]}"' not in html
    response = ui.post('/inbox/notices/read', json={'all': True, 'sprint': 'none', 'until': cutoff},
                       headers={'X-CSRF-Token': ui.csrf})
    assert response.json()['ids'] == [extras[2]]


@pytest.mark.parametrize('width', [390, 1440])
def test_notice_browser_selection_swipe_and_undo(env, ui, browser, server, width):  # noqa: F811
    from playwright.sync_api import expect

    from tests.test_ui_screens import _context, _login

    sid, cutoff, ids, extras = seed(env)
    ctx = _context(browser, server, (width, 844), width == 390)
    page = ctx.new_page()
    _login(page, server)
    page.goto(server + f'/?sprint={sid}')
    panel = page.locator('[data-notices]')
    panel.locator('details.inbox-notices-body').evaluate('(el) => { el.open = true; }')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    select = page.locator('.inbox-filter-right > select').bounding_box()
    assert select['x'] >= 0 and select['x'] + select['width'] <= width
    row = panel.locator(f'[data-notice-ids="{ids[0]}"]')
    if width == 390:
        row.dispatch_event('touchstart', {'touches': [{'identifier': 1, 'clientX': 270, 'clientY': 300}]})
        row.dispatch_event('touchend', {'changedTouches': [{'identifier': 1, 'clientX': 170, 'clientY': 305}]})
    else:
        row.hover()
    row.locator('[data-notice-read]').click()
    expect(row).to_be_hidden()
    panel.locator('[data-notice-undo]').click()
    expect(row).to_be_visible()
    panel.locator('[data-notice-select-mode]').click()
    row.locator('[data-notice-select]').check()
    expect(panel.locator('[data-notice-count]')).to_have_text('1 selected')
    page.keyboard.press('Escape')
    expect(panel.locator('.notice-selection')).to_be_hidden()
    panel.locator('[data-notice-select-mode]').click()
    panel.locator('[data-notice-select-all]').check()
    expect(panel.locator('[data-notice-count]')).to_have_text('3 selected')
    panel.locator('[data-notice-read-selected]').click()
    expect(panel.locator('[data-notice-ids]:visible')).to_have_count(0)
    expect(panel.locator('.notice-toast')).to_contain_text('3 notices marked read')
    panel.locator('[data-notice-undo]').click()
    expect(panel.locator('[data-notice-ids]:visible')).to_have_count(3)
    # The next action replaces the undo payload rather than accumulating IDs.
    panel.locator('[data-notice-read-all]').click()
    expect(panel.locator('[data-notice-ids]:visible')).to_have_count(0)
    panel.locator('[data-notice-undo]').click()
    expect(panel.locator('[data-notice-ids]:visible')).to_have_count(3)
    # Consecutive actions replace the undo payload: undoing the second keeps the first read.
    for notice_id in ids[:2]:
        item = panel.locator(f'[data-notice-ids="{notice_id}"]')
        if width == 390:
            item.evaluate("el => el.classList.add('swiped')")
        else:
            item.hover()
        item.locator('[data-notice-read]').click()
        expect(item).to_be_hidden()
    panel.locator('[data-notice-undo]').click()
    expect(row).to_be_hidden()
    expect(panel.locator(f'[data-notice-ids="{ids[1]}"]')).to_be_visible()
    panel.locator('[data-notice-select-mode]').click()
    panel.locator('[data-notice-cancel]').click()
    expect(panel.locator('.notice-selection')).to_be_hidden()
    page.clock.install()
    panel.locator('[data-notice-read-all]').click()
    expect(panel.locator('.notice-toast')).to_be_visible()
    page.clock.run_for(6100)
    expect(panel.locator('.notice-toast')).to_be_hidden()
    ctx.close()


def test_rendered_snapshot_preserves_same_second_arrivals(env, ui):  # noqa: F811
    import re

    sid, cutoff, ids, extras = seed(env)
    html = ui.get(f'/?sprint={sid}').text
    until = re.search(r'data-until="([^"]+)"', html)[1]
    max_id = int(re.search(r'data-max-id="(\d+)"', html)[1])
    with db.session() as s:
        original = s.get(Notification, ids[0])
        later = Notification(actor_id=original.actor_id, event=original.event, task_nr=original.task_nr,
                             created_at=datetime.fromisoformat(until).replace(tzinfo=None, microsecond=0))
        s.add(later)
        s.flush()
        later_id = later.id
        s.commit()
    response = ui.post('/inbox/notices/read', json={'all': True, 'sprint': str(sid), 'until': until, 'max_id': max_id},
                       headers={'X-CSRF-Token': ui.csrf})
    assert response.status_code == 200
    assert set(response.json()['ids']) == set(ids)
    with db.session() as s:
        assert s.get(Notification, later_id).read_at is None


def test_all_read_includes_notices_beyond_visible_page(env, ui):  # noqa: F811
    sid, cutoff, ids, extras = seed(env)
    with db.session() as s:
        original = s.get(Notification, ids[0])
        more = [Notification(actor_id=original.actor_id, event=original.event, task_nr=original.task_nr,
                             created_at=original.created_at - timedelta(minutes=i + 1)) for i in range(20)]
        s.add_all(more)
        s.flush()
        expected = set(ids + [n.id for n in more])
        owner = s.get(Actor, original.actor_id)
        assert len(notifications.notices_for(s, owner, limit=15, sprint=str(sid))) == 15
        s.commit()
    headers = {'X-CSRF-Token': ui.csrf}
    read = ui.post('/inbox/notices/read', json={'all': True, 'sprint': str(sid), 'until': cutoff}, headers=headers)
    assert set(read.json()['ids']) == expected
    undo = ui.post('/inbox/notices/unread', json={'ids': read.json()['ids']}, headers=headers)
    assert set(undo.json()['ids']) == expected


@pytest.mark.parametrize('role', ['admin', 'member'])
@pytest.mark.parametrize('proxied', [False, True])
def test_browser_session_notice_actions(env, role, proxied):
    """Real login cookie + rendered token + fetch headers, including HTTPS termination."""
    import json
    import re
    from html import unescape

    from fastapi.testclient import TestClient

    from app.auth import hash_password
    from app.main import app
    from app.models import Project
    from tests.conftest import BOOTSTRAP_PW

    sid, cutoff, ids, extras = seed(env)
    username, password = 'admin', BOOTSTRAP_PW
    if role == 'member':
        username, password = 'notice-member', 'notice-member-password'
        with db.session() as s:
            member = Actor(name=username, display_name='Notice member', actor_type='human', role=role,
                           password_hash=hash_password(password))
            s.add(member)
            s.flush()
            s.scalar(select(Project).where(Project.project_key == 'KET')).owner_id = member.id
            for notice_id in [*ids, extras[0], extras[2], extras[3]]:
                s.get(Notification, notice_id).actor_id = member.id
            s.commit()
    # The public Host is preserved, but Uvicorn sees HTTP with proxy headers disabled.
    with TestClient(app, base_url='http://taskbrigade.example.ts.net') as c:
        html = c.get('/login').text
        token = re.search(r'name="csrf_token" value="([^"]+)"', html)[1]
        response = c.post('/login', data={'username': username, 'password': password, 'csrf_token': token},
                          follow_redirects=False)
        assert response.status_code == 303
        html = c.get(f'/?sprint={sid}').text
        headers = json.loads(unescape(re.search(r"hx-headers='([^']+)'", html)[1]))
        headers.update({'Content-Type': 'application/json', 'Sec-Fetch-Site': 'same-origin',
                        'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty',
                        'Origin': ('https' if proxied else 'http') + '://taskbrigade.example.ts.net'})
        assert c.cookies.get('tm_session')
        assert 'Authorization' not in c.headers
        for payload, expected in [({'ids': ids[:1]}, ids[:1]), ({'ids': ids[:2]}, ids[:2]),
                                  ({'all': True, 'sprint': str(sid),
                                    'until': re.search(r'data-until="([^"]+)"', html)[1],
                                    'max_id': int(re.search(r'data-max-id="(\d+)"', html)[1])}, ids)]:
            read = c.post('/inbox/notices/read', json=payload, headers=headers)
            assert read.status_code == 200, read.text
            assert set(read.json()['ids']) == set(expected)
            with db.session() as s:
                assert all(s.get(Notification, i).read_at for i in expected)
                assert s.get(Notification, extras[1]).read_at is None
            undo = c.post('/inbox/notices/unread', json={'ids': read.json()['ids']}, headers=headers)
            assert undo.status_code == 200, undo.text
            assert set(undo.json()['ids']) == set(expected)
            with db.session() as s:
                assert all(s.get(Notification, i).read_at is None for i in expected)
                assert s.get(Notification, extras[3]).read_at is not None
        for endpoint in ('read', 'unread'):
            for bad_headers in ({**headers, 'X-CSRF-Token': ''},
                                {**headers, 'Sec-Fetch-Site': 'cross-site', 'Origin': 'https://evil.example'},
                                {**headers, 'Sec-Fetch-Site': 'same-site'}):
                assert c.post('/inbox/notices/' + endpoint, json={'ids': ids}, headers=bad_headers).status_code == 403


@pytest.mark.parametrize('failure', ['http', 'network'])
def test_notice_browser_readable_errors(env, ui, browser, server, failure):  # noqa: F811
    from playwright.sync_api import expect

    from tests.test_ui_screens import _context, _login

    sid, cutoff, ids, extras = seed(env)
    ctx = _context(browser, server, (1440, 900), False)
    ctx.add_cookies([{'name': 'tm_lang', 'value': 'de', 'url': server}])
    page = ctx.new_page()
    _login(page, server)
    page.goto(server + f'/?sprint={sid}')
    panel = page.locator('[data-notices]')
    row = panel.locator(f'[data-notice-ids="{ids[0]}"]')
    panel.locator('details.inbox-notices-body').evaluate('(el) => { el.open = true; }')
    row.hover()
    row.locator('[data-notice-read]').click()
    expect(row).to_be_hidden()

    def fail(route):
        if failure == 'http':
            route.fulfill(status=403, content_type='application/json', body='{}')
        else:
            route.abort()

    page.route('**/inbox/notices/*', fail)
    panel.locator('[data-notice-undo]').click()
    message = panel.locator('#inbox-notices-msg')
    expect(message).to_have_text('Konnte das Markieren der Hinweise als gelesen nicht rückgängig machen – bitte neu laden.')
    expect(row).to_be_hidden()
    panel.locator('[data-notice-read-all]').click()
    expect(message).to_have_text('Konnte Hinweise nicht als gelesen markieren – bitte neu laden.')
    expect(panel.locator(f'[data-notice-ids="{ids[1]}"]')).to_be_visible()
    expect(panel).not_to_have_attribute('aria-busy', 'true')
    ctx.close()
