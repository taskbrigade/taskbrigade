"""Shared header rendering independent of a database connection."""
from types import SimpleNamespace

import pytest

from app.i18n import use_language
from app.web.common import inbox_badge, templates


def header(admin=True, ready=True, actor_id=1, **values):
    context = dict(request=SimpleNamespace(url=SimpleNamespace(path='/backlog', query='q=hello&prio=1'), cookies={}),
                   user=SimpleNamespace(name='demo', is_admin=admin, actor_id=actor_id), db_ready=ready,
                   qc_projects=[('DEMO', 'Demo project')], csrf='test-csrf', inbox_badge=3,
                   show_planning=True, show_workflows=True, show_data=True)
    context.update(values)
    return templates.get_template('base.html').render(**context)


@pytest.mark.parametrize('lang', ['en', 'de'])
def test_header_preserves_routes_and_account_actions(lang):
    with use_language(lang):
        html = header()
    assert 'href="/backlog" aria-current="page"' in html
    assert 'aria-controls="account-menu"' in html
    assert 'action="/logout"' in html and 'name="csrf_token" value="test-csrf"' in html
    assert 'href="/profile"' in html
    assert 'lang=en&amp;next=' in html and 'lang=de&amp;next=' in html
    assert 'action="/tasks/quick"' in html and 'name="title"' in html
    assert 'name="project"' in html and 'value="DEMO"' in html
    # Own project select: "KEY – name" in the list, the key alone on the closed control, name as tooltip.
    assert '>DEMO – Demo project</option>' in html and 'class="qc-project-key" aria-hidden="true">DEMO<' in html
    assert 'class="qc-project" title="Demo project"' in html
    assert f'class="qc-project-label">{"Projekt" if lang == "de" else "Project"}<' in html
    assert 'class="bottom-tabs"' in html and 'aria-controls="more-menu"' in html
    assert '>3</span>' in html
    assert 'Log out demo' not in html and 'demo abmelden' not in html


def test_header_hides_unavailable_features_and_admin():
    html = header(admin=False, show_planning=False, show_workflows=False, show_data=False)
    for route in ('/admin/', '/planning', '/workflows', '/data'):
        assert f'href="{route}' not in html
    assert 'href="/projects"' in html
    html = header(ready=False, actor_id=None)
    assert 'href="/admin/database"' in html
    assert 'action="/tasks/quick"' not in html and 'class="bottom-tabs"' not in html


def test_badge_matches_inbox_action_categories():
    assert inbox_badge(None, {'inbox': {'questions': [1], 'for_review': [1, 2], 'aborted': [1],
                                      'workflow_questions': [1], 'unassigned': [1]},
                             'mail_approvals': [1], 'data_releases': [1]}) == 6
    # Reviews by reviewer agents do not need the user: own Inbox section, not counted.
    assert inbox_badge(None, {'inbox': {'for_review': [1], 'agent_review': [1, 2], 'agent_decided': [1]}}) == 1
