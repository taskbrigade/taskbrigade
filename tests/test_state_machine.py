"""State machine: every allowed and forbidden transition without a DB."""
import itertools

import pytest

from app.errors import Forbidden
from app.models import TASK_STATUSES, Actor, Task
from app.services.state_machine import (GENERIC_ACTIONS, RULES, InvalidTransition, branch_name_for,
                                        check_allowed, find_generic_rule, slugify)

ASSIGNEE, OTHER_AGENT, CREATOR = 10, 11, 12

# Expected transitions from spec section 6: (action, from) → to.
EXPECTED = {
    ("claim", "defined"): "in_progress",
    ("claim", "changes_requested"): "in_progress",
    ("ask", "in_progress"): "questions",
    ("answer", "questions"): "defined",
    ("submit", "in_progress"): "for_review",
    ("approve", "for_review"): "done",
    ("reject", "for_review"): "changes_requested",
    ("reject_conflict", "for_review"): "changes_requested",
    ("release", "in_progress"): "defined",
    ("lease_expired", "in_progress"): "defined",
    **{("cancel", s): "cancelled" for s in ("draft", "defined", "in_progress", "questions", "aborted", "for_review",
                                            "changes_requested")},
    # Draft: admin/orchestrator/creator may publish; return to draft only before claiming.
    ("publish", "draft"): "defined",
    ("to_draft", "defined"): "draft",
    # Failed agent → awaits reassignment; only humans reopen it.
    **{("abort", s): "aborted" for s in ("in_progress", "questions", "changes_requested", "for_review")},
    ("reopen", "aborted"): "defined",
}
ACTORS = ("assignee", "other", "creator", "admin", "agent_admin", "orchestrator", "system")


def allowed(action: str, status: str, who: str) -> bool:
    """Permission matrix from spec section 6 plus the aborted-agent extension."""
    return {
        "claim": who == "assignee", "ask": who == "assignee", "submit": who == "assignee",
        "release": who == "assignee", "lease_expired": who == "system",
        "answer": who in ("admin", "agent_admin"), "approve": who in ("admin", "agent_admin"),
        "reject": who in ("admin", "agent_admin"),
        "reject_conflict": who in ("admin", "agent_admin"),
        "cancel": who in ("admin", "agent_admin", "orchestrator"),
        "abort": who == "admin" or (who == "assignee" and status == "in_progress"),
        "reopen": who == "admin",
        "publish": who in ("admin", "agent_admin", "orchestrator", "creator"),
        "to_draft": who in ("admin", "agent_admin", "orchestrator"),
    }[action]


def actor(kind: str) -> Actor | None:
    return {
        "assignee": Actor(id=ASSIGNEE, name="bot", role="worker", actor_type="agent"),
        "other": Actor(id=OTHER_AGENT, name="bot2", role="worker", actor_type="agent"),
        "creator": Actor(id=CREATOR, name="anleger", role="worker", actor_type="agent"),
        "admin": Actor(id=1, name="admin", role="admin", actor_type="human"),
        "agent_admin": Actor(id=3, name="botadmin", role="admin", actor_type="agent"),
        "orchestrator": Actor(id=2, name="hermes", role="orchestrator", actor_type="agent"),
        "system": None,
    }[kind]


def task(status: str) -> Task:
    return Task(id=1, task_nr="KET-0001", title="Test", status=status, assigned_to=ASSIGNEE, created_by=CREATOR)


def test_rules_match_spec():
    actual = {(a, s): r.target for a, r in RULES.items() for s in r.sources}
    assert actual == EXPECTED


def some_allowed_actor(action: str, status: str) -> str:
    return next(w for w in ACTORS if allowed(action, status, w))


@pytest.mark.parametrize("action,status", sorted(itertools.product(RULES, TASK_STATUSES)))
def test_every_action_from_every_status(action, status):
    """Unexpected source statuses return 409 for every actor."""
    if (action, status) in EXPECTED:
        check_allowed(RULES[action], task(status), actor(some_allowed_actor(action, status)))
    else:
        who = actor(some_allowed_actor(action, next(s for (a, s) in EXPECTED if a == action)))
        with pytest.raises(InvalidTransition):
            check_allowed(RULES[action], task(status), who)


@pytest.mark.parametrize("action,status", sorted(EXPECTED))
@pytest.mark.parametrize("who", ACTORS)
def test_role_permissions(action, status, who):
    if allowed(action, status, who):
        check_allowed(RULES[action], task(status), actor(who))
    else:
        with pytest.raises((Forbidden, InvalidTransition)):
            check_allowed(RULES[action], task(status), actor(who))


@pytest.mark.parametrize("src,dst", sorted(itertools.permutations(TASK_STATUSES, 2)))
def test_generic_transition_mapping(src, dst):
    """Kanban/`/transition` allows generic actions; others require admin override."""
    rule = find_generic_rule(src, dst)
    expected = [a for (a, s), t in EXPECTED.items() if s == src and t == dst and a in GENERIC_ACTIONS]
    assert (rule.action if rule else None) == (expected[0] if expected else None)


def test_end_states_have_no_exit():
    for status in ("done", "cancelled"):
        assert not [a for (a, s) in EXPECTED if s == status]


@pytest.mark.parametrize("title,slug", [
    ("Login-Seite bauen", "login-seite-bauen"),
    ("Größe ändern & Übergänge prüfen!!", "groesse-aendern-uebergaenge-pruefen"),
    ("   ", "task"),
    ("x" * 60, "x" * 40),
    ("Ein sehr langer Titel, der am Ende abgeschnitten wird", "ein-sehr-langer-titel-der-am-ende-abgesc"),
])
def test_slugify(title, slug):
    assert slugify(title) == slug
    assert len(slug) <= 40 and not slug.endswith("-")


def test_branch_name():
    assert branch_name_for(Task(task_nr="KET-0042", title="Foo Bar")) == "task/KET-0042-foo-bar"
