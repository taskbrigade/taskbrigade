"""Compact actor overview, presence boundaries and safe detail editing."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.datastructures import URL

from app.i18n import track_missing, use_language
from app.models import Actor
from app.web import admin_users as users
from app.web.common import templates

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def actor(**values):
    fields = dict(id=2, name='demo-agent', display_name='Demo agent', actor_type='agent',
                  agent_kind='codex', role='worker', runtime='local', model_location='cloud',
                  is_active=True, is_public=False, max_parallel_tasks=3, last_seen_at=NOW,
                  api_key_hash='hash-only', api_key_prefix='tm_demo', api_key_created_at=NOW,
                  gitea_username='demo-git', gitea_token_checked_at=NOW, owner_id=1)
    fields.update(values)
    return Actor(**fields)


@pytest.mark.parametrize('seconds,state', [(0, 'online'), (899, 'online'), (900, 'idle'),
                                          (86399, 'idle'), (86400, 'offline')])
def test_presence_boundaries(seconds, state):
    a = actor(last_seen_at=(NOW - timedelta(seconds=seconds)).replace(tzinfo=None))
    assert users.presence(a, NOW)['state'] == state


def test_presence_missing_inactive_and_future():
    with use_language('en'):
        assert users.presence(actor(last_seen_at=None), NOW) == {
            'state': 'offline', 'label': 'Never', 'status': 'Offline'}
        assert users.presence(actor(is_active=False), NOW)['state'] == 'offline'
        assert users.presence(actor(last_seen_at=NOW + timedelta(seconds=10)), NOW)['label'] == 'Just now'
        assert users.presence(actor(last_seen_at=NOW - timedelta(minutes=5)), NOW)['label'] == '5 min ago'


@pytest.mark.parametrize('query,expected', [('DEMO-GIT', True), ('codex', True), ('Demo agent', True),
                                          ('absent', False)])
def test_search_and_type_filters(query, expected):
    filters = dict(owner='', role='', kind='', q=query, type='agent')
    assert users._matches(actor(), 'owner', filters) == expected
    filters['type'] = 'human'
    assert not users._matches(actor(), 'owner', filters)


def render_users(lang, selected=True):
    a = actor()
    url = URL('http://testserver/admin/users')
    request = SimpleNamespace(url=url, url_for=lambda name: url, cookies={})
    filters = dict(owner='', role='', kind='', q='', type='')
    context = dict(request=request, user=None, csrf='test-csrf', db_ready=True, section='users',
                   list_url=url, actors=[a], actor=a if selected else None, total=1,
                   counts={2: {'running': 1, 'open': 4}}, owners={2: 'demo-owner'},
                   presence={2: users.presence(a, NOW)}, zone_max={2: 'cloud_ok'},
                   groups=[dict(owner='demo-owner', agents=True, rows=[a], online=1)], humans=['demo-owner'],
                   type_counts=dict(agent=1, human=0), roles=['worker', 'reviewer'], kinds=['codex'],
                   filters=filters, filtered=False)
    with use_language(lang), track_missing() as missing:
        html = templates.get_template('admin/users.html').render(**context)
    assert not missing
    return html


@pytest.mark.parametrize('lang', ['en', 'de'])
def test_overview_and_panel_render_without_secrets(lang):
    html = render_users(lang)
    assert html.count('<th>') == 7
    assert 'demo-owner' in html and 'ui-dot-online' in html
    assert 'hash-only' not in html and 'tm_demo••••' in html
    assert 'name="csrf_token" value="test-csrf"' in html
    assert 'action="/admin/users/demo-agent/summary?panel=1"' in html
    for action in ('rotate-token', 'revoke-token', 'gitea/check', 'gitea', 'active'):
        assert f'action="/admin/users/demo-agent/{action}?panel=1"' in html
    assert 'id="user-panel-close"' in html and 'id="user-summary"' in html
    assert '/admin/users/new?type=human' in html
    assert 'user-panel-title' not in render_users(lang, selected=False)


def test_summary_changes_only_compact_fields(monkeypatch):
    a = actor()
    session = Mock()

    @contextmanager
    def web_session():
        yield session

    monkeypatch.setattr(users, 'web_session', web_session)
    monkeypatch.setattr(users.svc, 'get', lambda s, name: a)
    update = Mock(return_value=['role'])
    monkeypatch.setattr(users.svc, 'update', update)
    monkeypatch.setattr(users, 'audit', Mock())
    monkeypatch.setattr(users, '_detail', lambda *args, **kwargs: kwargs)
    request = SimpleNamespace(query_params={'panel': '1'})
    user = SimpleNamespace(name='admin')
    users.update_summary(request, a.name, user, role='reviewer', runtime='cloud',
                         owner='demo-owner', max_parallel_tasks='4')
    assert update.call_args.args[2] == dict(role='reviewer', runtime='cloud', owner='demo-owner',
                                          max_parallel_tasks=4)
    users.revoke(request, a.name, user)
    assert a.api_key_hash is a.api_key_prefix is a.api_key_created_at is None
    assert a.is_active


@pytest.mark.db
def test_panel_actions_and_permissions(ready_db, respx_mock):
    import httpx
    from fastapi.testclient import TestClient

    from app.db import db
    from app.main import app
    from app.services import actors, settings_store
    from tests.conftest import login

    with db.session() as s:
        a = actors.get(s, 'codex')
        token = actors.rotate_token(s, a)
        a.notes = 'Preserve advanced settings'
        a.model_default = 'demo-model'
        a.owner_id = actors.get(s, 'admin').id
        settings_store.update(s, {'gitea_url': 'http://gitea.example.org'}, None)
        s.commit()
    with TestClient(app) as ui:
        # Both compact mutations retain the admin guard.
        assert ui.post('/admin/users/codex/revoke-token', follow_redirects=False).status_code == 303
        login(ui)
        page = ui.get('/admin/users?detail=codex').text
        assert token not in page and 'user-panel-title' in page
        csrf = page.split('name="csrf_token" value="')[1].split('"')[0]
        path = '/admin/users/codex/summary?panel=1'
        assert ui.post(path, data=dict(role='reviewer', runtime='cloud', max_parallel_tasks='3')).status_code == 403
        response = ui.post(path, data=dict(csrf_token=csrf, role='reviewer', runtime='cloud',
                                          max_parallel_tasks='3', owner='none'))
        assert response.status_code == 200 and 'user-panel-title' in response.text
        assert '/summary?panel=1&amp;detail=' not in response.text
        with db.session() as s:
            a = actors.get(s, 'codex')
            assert (a.role, a.max_parallel_tasks, a.owner_id) == ('reviewer', 3, None)
            assert a.notes == 'Preserve advanced settings' and a.model_default == 'demo-model'
        response = ui.post(path, data=dict(csrf_token=csrf, role='reviewer', runtime='cloud',
                                          max_parallel_tasks='0'))
        assert response.status_code == 422 and 'user-panel-title' in response.text
        respx_mock.get('http://gitea.example.org/api/v1/user').mock(
            return_value=httpx.Response(200, json={'login': 'demo-git', 'email': ''}))
        response = ui.post('/admin/users/codex/gitea?panel=1', data=dict(
            csrf_token=csrf, gitea_username='demo-git', gitea_token='test-gitea-value'))
        assert response.status_code == 200 and 'user-panel-title' in response.text
        assert 'test-gitea-value' not in response.text
        assert ui.post('/admin/users/codex/gitea/check?panel=1', data=dict(csrf_token=csrf)).status_code == 200
        response = ui.post('/admin/users/codex/revoke-token?panel=1', data=dict(csrf_token=csrf))
        assert response.status_code == 200
        with db.session() as s:
            assert actors.by_token(s, token) is None
            assert actors.get(s, 'codex').is_active
        response = ui.post('/admin/users/codex/rotate-token?panel=1', data=dict(csrf_token=csrf))
        assert response.status_code == 200 and 'type="password" id="new-api-token"' in response.text
        assert response.headers['cache-control'] == 'no-store'
        with db.session() as s:
            a = actors.get(s, 'codex')
            assert f'{a.api_key_prefix}••••' in response.text
        assert 'Copy token' in response.text or 'Token kopieren' in response.text
        page = ui.get('/admin/users?detail=codex').text
        assert 'id="new-api-token"' not in page
        page = ui.get('/admin/users/new?type=human')
        assert page.status_code == 200 and 'value="human" selected' in page.text
        assert 'value="member" selected' in page.text

        response = ui.post('/admin/users/new', data=dict(csrf_token=csrf, name='demo-member',
            display_name='Demo member', actor_type='human', role='member', runtime='local',
            password='test-member-password', password2='test-member-password'))
        assert response.status_code == 200
        with db.session() as s:
            assert actors.get(s, 'demo-member').actor_type == 'human'
