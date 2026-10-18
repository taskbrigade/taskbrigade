"""Backlog edits use ordinary domain rules and isolate per-row failures."""
import pytest

from app.db import db
from app.models import Actor, Project, Sprint, Task
from tests.test_ui_cli import env, ui, gitea, mkproject, mktask  # noqa: F401

pytestmark = pytest.mark.db


def rows(*tasks):
    with db.session() as s:
        return [{"task_nr": nr, "row_version": s.query(Task).filter_by(task_nr=nr).one().row_version}
                for nr in tasks]


def bulk(ui, action, value, tasks, **kwargs):
    return ui.post('/backlog/bulk', json={"action": action, "value": value, "tasks": tasks, **kwargs},
                   headers={"X-CSRF-Token": ui.csrf})


def test_bulk_assign_priority_and_clear(env, ui):
    mkproject(env)
    tasks = [mktask(env)["task_nr"] for _ in range(2)]
    for action, value in (("assigned_to", "codex"), ("prio", "1"), ("assigned_to", "")):
        r = bulk(ui, action, value, rows(*tasks))
        assert r.status_code == 200 and all(x['ok'] for x in r.json()['results']), r.text
    with db.session() as s:
        for nr in tasks:
            task = s.query(Task).filter_by(task_nr=nr).one()
            assert task.prio == 1 and task.assigned_to is None


def test_bulk_sprints_validate_each_project(env, ui):
    mkproject(env)
    mkproject(env, key="OTHER")
    first = mktask(env)["task_nr"]
    other = mktask(env, project="OTHER")["task_nr"]
    with db.session() as s:
        sprint = Sprint(project_id=s.query(Project).filter_by(project_key="KET").one().id,
                        name="Backlog wave", status="planned", created_by=s.query(Actor).filter_by(name="admin").one().id)
        s.add(sprint)
        s.commit()
        sid = sprint.id
    result = bulk(ui, 'sprint', str(sid), rows(first, other)).json()['results']
    assert result[0]['ok'] and not result[1]['ok']
    with db.session() as s:
        assert s.query(Task).filter_by(task_nr=first).one().sprint_id == sid
        assert s.query(Task).filter_by(task_nr=other).one().sprint_id is None
    assert bulk(ui, 'sprint', 'none', rows(first)).json()['results'][0]['ok']


def test_bulk_stale_missing_invalid_and_success_are_independent(env, ui):
    mkproject(env)
    first, second = [mktask(env)["task_nr"] for _ in range(2)]
    snapshot = rows(first, second)
    assert bulk(ui, 'prio', '2', rows(first)).json()['results'][0]['ok']
    r = bulk(ui, 'prio', '1', [*snapshot, {"task_nr": "KET-9999", "row_version": 1}])
    result = r.json()['results']
    assert result[0]['error']['code'] == 'version_conflict'
    assert result[1]['ok'] and result[2]['error']['status'] == 404
    result = bulk(ui, 'prio', '0', rows(first, second)).json()['results']
    assert all(not x['ok'] for x in result)


def test_bulk_status_uses_transition_rules(env, ui):
    mkproject(env)
    first = mktask(env, status="draft")["task_nr"]
    second = mktask(env, status="draft", description="", acceptance_criteria="")["task_nr"]
    result = bulk(ui, 'status', 'defined', rows(first, second)).json()['results']
    assert result[0]['ok'] and not result[1]['ok']
    assert result[1]['error']['code'] == 'definition_incomplete'
    # Even an admin gets no silent override to an arbitrary state.
    result = bulk(ui, 'status', 'done', rows(first)).json()['results']
    assert not result[0]['ok']


def test_bulk_edit_permissions_and_csrf(env, ui):
    mkproject(env)
    nr = mktask(env)["task_nr"]
    snapshot = rows(nr)
    assert ui.post('/backlog/bulk', json={"action": "prio", "value": "1", "tasks": snapshot}).status_code == 403
    # Ordinary human workers cannot edit other owners' tasks.
    with db.session() as s:
        actor = s.query(Actor).filter_by(name="admin").one()
        actor.role = 'worker'
        s.query(Project).filter_by(project_key="KET").one().owner_id = s.query(Actor).filter_by(name="hermes").one().id
        s.commit()
    result = bulk(ui, 'prio', '1', snapshot).json()['results']
    assert result[0]['error']['status'] == 403


def test_backlog_counts_status_selection_sorting_and_pages(env, ui):
    mkproject(env)
    draft = mktask(env, title="Backlog draft", status="draft", prio=4)["task_nr"]
    defined = mktask(env, title="Backlog defined", prio=1)["task_nr"]
    html = ui.get('/backlog?status=draft&workflow=hide').text
    assert draft in html and defined not in html
    assert 'value="defined"' in html and 'ui-chip-count">1</span>' in html
    assert 'data-task-select' in html and 'backlog-dialog' in html
    html = ui.get('/backlog?status_set=1').text
    assert 'data-task-select' not in html
    for sort, expected in (("task_nr", [draft, defined]), ("prio", [defined, draft]), ("updated_at", [draft, defined])):
        html = ui.get(f'/backlog?sort={sort}&desc=0').text
        assert html.index(f'value="{expected[0]}"') < html.index(f'value="{expected[1]}"')
    html = ui.get('/backlog?page=2&limit=25').text
    assert 'data-task-select' not in html and 'page=1' in html
    partial = ui.get('/backlog?prio=1', headers={"HX-Request": "true", "HX-Target": "backlog-content"}).text
    assert '<html' not in partial and 'ui-chip-count' in partial and defined in partial


def test_bulk_failed_reassignment_rolls_back_reset_and_events(env, ui):
    mkproject(env)
    nr = mktask(env, assigned_to="codex")["task_nr"]
    response = env["c"].post('/api/v1/agents/me/claim', headers=env["codex"], json={"task_nr": nr})
    assert response.status_code == 200, response.text
    with db.session() as s:
        s.query(Actor).filter_by(name="hermes").one().is_active = False
        s.commit()
    snapshot = rows(nr)
    result = bulk(ui, 'assigned_to', 'hermes', snapshot).json()['results'][0]
    assert not result['ok']
    with db.session() as s:
        task = s.query(Task).filter_by(task_nr=nr).one()
        assert task.status == 'in_progress' and task.lease_expires_at is not None
        assert task.assigned_to == s.query(Actor).filter_by(name="codex").one().id
        assert task.row_version == snapshot[0]['row_version']


def test_backlog_key_sort_across_projects(env, ui):
    mkproject(env)
    first = mktask(env)["task_nr"]
    mkproject(env, key="AAA")
    alphabetic_first = mktask(env, project="AAA")["task_nr"]
    html = ui.get('/backlog?owner=all&sort=task_nr').text
    assert html.index(f'value="{alphabetic_first}"') < html.index(f'value="{first}"')
