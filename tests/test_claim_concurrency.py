"""Claim concurrency against real MariaDB (spec section 15: two parallel claims → one winner)."""
import threading
from collections import Counter

import pytest
from sqlalchemy import select, text

from app.db import db
from app.models import Actor, Task
from app.services import tasks
from tests.test_workflow import env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)

pytestmark = pytest.mark.db


def parallel_claims(agent: str, n: int) -> list[str | None]:
    """Start n threads with separate DB connections claiming simultaneously."""
    barrier = threading.Barrier(n)
    results: list[str | None] = [None] * n
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            with db.session() as s:
                me = s.scalar(select(Actor).where(Actor.name == agent))
                barrier.wait(timeout=10)
                task = tasks.claim(s, me, skip_locked=db.skip_locked)
                results[i] = task.task_nr if task else None
                s.commit()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    return results


def claim_events(nr: str) -> int:
    with db.engine.connect() as conn:
        return conn.execute(text(
            "SELECT COUNT(*) FROM task_events e JOIN tasks t ON t.id = e.task_id "
            "WHERE t.task_nr = :n AND e.event_type = 'status_change' AND e.to_value = 'in_progress'"),
            {"n": nr}).scalar()



@pytest.mark.parametrize("n", [2, 5])
def test_parallel_claims_single_task_exactly_one_wins(env, n):  # noqa: F811
    mkproject(env)
    nr = mktask(env, assigned_to="claude-code")["task_nr"]
    results = parallel_claims("claude-code", n)
    assert Counter(results) == Counter({nr: 1, None: n - 1})
    assert claim_events(nr) == 1


def test_parallel_claims_respect_max_parallel(env):  # noqa: F811
    mkproject(env)
    for _ in range(3):
        mktask(env, assigned_to="claude-code")
    results = parallel_claims("claude-code", 4)
    assert sum(r is not None for r in results) == 1  # max_parallel_tasks = 1

    env["c"].patch("/api/v1/admin/actors/claude-code", headers=env["admin"], json={"max_parallel_tasks": 3})
    results = parallel_claims("claude-code", 4)
    won = [r for r in results if r]
    assert len(won) == 2 and len(set(won)) == 2  # The two remaining tasks, with no duplicates.
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM tasks WHERE status='in_progress'")).scalar() == 3


def test_different_agents_claim_in_parallel(env):  # noqa: F811
    mkproject(env)
    a = mktask(env, assigned_to="claude-code")["task_nr"]
    h = mktask(env, assigned_to="hermes")["task_nr"]
    out: dict[str, list] = {}
    ts = [threading.Thread(target=lambda n=n: out.__setitem__(n, parallel_claims(n, 2)))
          for n in ("claude-code", "hermes")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(filter(None, out["claude-code"])) == [a]
    assert sorted(filter(None, out["hermes"])) == [h]
    with db.session() as s:
        assert {t.status for t in s.scalars(select(Task))} == {"in_progress"}


def test_parallel_task_creation_unique_numbers(env):  # noqa: F811
    """Allocate task numbers under the project row lock: parallel creates yield unique consecutive numbers."""
    mkproject(env)
    n = 6
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker():
        try:
            with db.session() as s:
                me = s.scalar(select(Actor).where(Actor.name == "admin"))
                from app.services import projects
                projects.get(s, "KET")  # Load project early without lock, as in the API path.
                barrier.wait(timeout=10)
                results.append(tasks.create(s, me, project="KET", title="parallel",
                                                description="x", acceptance_criteria="- y").task_nr)
                s.commit()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert sorted(results) == [f"KET-{i:04d}" for i in range(1, n + 1)]
