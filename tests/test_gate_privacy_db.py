"""Release accounting, human review, isolation and concurrent budget exhaustion on MariaDB."""
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier

import pytest
from sqlalchemy import select

from app.db import db
from app.errors import Conflict
from app.models import Actor, Dataset, RunResult
from app.services import gate_privacy as gp, output_gate
from tests.test_output_gate import audit, decide, genv, latest, run  # noqa: F401
from tests.test_runner_jobs import renv  # noqa: F401
from tests.test_data_store import denv  # noqa: F401
from tests.test_workflow import env, gitea  # noqa: F401

pytestmark = pytest.mark.db


def configure(e, **fields):
    r = e["alex_ui"].post(f'/data/{e["customers"]}/output-policy',
                           data={"csrf_token": e["alex_ui"].csrf, **fields}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert "err=" not in r.headers["location"], r.headers["location"]


def usage(e):
    with db.session() as s:
        ds = s.scalar(select(Dataset).where(Dataset.dataset_key == e["customers"]))
        return gp.view(ds)


def noisy_release(e, result, token=None, reviewed="1"):
    return e["alex_ui"].post(f"/data/results/{result.id}/decide", data={
        "csrf_token": e["alex_ui"].csrf, "decision": "release", "noise_reviewed": reviewed,
        "policy_token": token if token is not None else usage(e)["token"]}, follow_redirects=False)


def test_default_release_count_and_unchanged_output(genv):
    nr, result = run(genv)
    assert "err=" not in decide(genv["alex_ui"], result.id, "release").headers["location"]
    stored = latest(nr)
    assert json.loads(stored.released_json)["metrics"]["rows"] == 12
    assert usage(genv)["released_count"] == 1 and Decimal(usage(genv)["epsilon_spent"]) == 0
    assert json.loads(stored.release_policy_json)["mechanism"] == "none"


def test_count_budget_exhaustion_is_displayed_and_enforced(genv):
    configure(genv, query_limit="1")
    nr1, first = run(genv)
    nr2, second = run(genv)
    assert "err=" not in decide(genv["alex_ui"], first.id, "release").headers["location"]
    page = genv["alex_ui"].get(f"/data/results/{second.id}").text
    assert "Abfragebudget" in page and 'value="release"' not in page
    assert "err=" in decide(genv["alex_ui"], second.id, "release").headers["location"]
    assert latest(nr2).released_json is None and latest(nr1).status == "released"
    assert usage(genv)["released_count"] == 1
    # Rejection/keeping sealed remains available at zero remaining budget, without a charge.
    assert "err=" not in decide(genv["alex_ui"], second.id, "keep_sealed").headers["location"]
    assert usage(genv)["released_count"] == 1


def test_dataset_budgets_are_independent(genv):
    configure(genv, query_limit="0")
    nr, result = run(genv, dataset=genv["dataset"])
    assert "err=" not in decide(genv["alex_ui"], result.id, "release").headers["location"]
    assert latest(nr).status == "released" and usage(genv)["released_count"] == 0
    with db.session() as s:
        other = s.scalar(select(Dataset).where(Dataset.dataset_key == genv["dataset"]))
        assert other.released_count == 1


def test_noise_requires_current_policy_confirmation_then_charges_epsilon(genv, monkeypatch):
    configure(genv, epsilon="0.1", sensitivity="4", epsilon_limit="0.2")
    nr, result = run(genv)
    stale = usage(genv)["token"]
    configure(genv, epsilon="0.2", sensitivity="4", epsilon_limit="0.2")
    assert "err=" in noisy_release(genv, result, token=stale).headers["location"]
    assert "err=" in noisy_release(genv, result, reviewed="").headers["location"]
    assert latest(nr).released_json is None and usage(genv)["released_count"] == 0
    monkeypatch.setattr(gp, "laplace", lambda scale: 2.375)
    assert "err=" not in noisy_release(genv, result).headers["location"]
    stored = latest(nr)
    content = json.loads(stored.released_json)
    assert content["metrics"]["rows"] == 14.38
    assert all(r[1] == 8.38 for r in content["tables"][0]["rows"])
    assert Decimal(usage(genv)["epsilon_spent"]) == Decimal("0.2")
    release_policy = json.loads(stored.release_policy_json)
    assert release_policy["epsilon"] == "0.200000" and release_policy["sensitivity_reviewed"]
    assert json.loads(audit("result_released")[-1].detail_json)["release_policy"] == release_policy
    # Reads use saved noise; a later policy edit cannot rewrite or re-randomize this artifact.
    monkeypatch.setattr(gp, "laplace", lambda scale: pytest.fail("fresh noise on read"))
    configure(genv)
    genv["alex_ui"].get(f"/data/results/{result.id}")
    assert latest(nr).released_json == stored.released_json
    configure(genv, epsilon="0.1", sensitivity="4", epsilon_limit="0.2")
    nr2, result2 = run(genv)
    history_check = next(c for c in json.loads(result2.checks_json)["checks"] if c["n"] == 8)
    assert history_check["status"] == "warning" and history_check["code"] == "noisy_history"
    assert "err=" in noisy_release(genv, result2).headers["location"]
    assert latest(nr2).released_json is None


def test_policy_changes_owner_only_validated_audited_and_csrf_protected(genv):
    path = f'/data/{genv["customers"]}/output-policy'
    assert genv["alex_ui"].post(path, data={"query_limit": "0"}).status_code == 403
    for c in (genv["bob_ui"],):  # bob has data rights, but does not own the project
        r = c.post(path, data={"csrf_token": c.csrf, "query_limit": "0"}, follow_redirects=False)
        assert "err=" in r.headers["location"]
    assert usage(genv)["query_limit"] is None
    r = genv["alex_ui"].post(path, data={"csrf_token": genv["alex_ui"].csrf, "epsilon": "NaN",
                                          "sensitivity": "1"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and not audit("gate_policy_changed")
    configure(genv, query_limit="0")
    assert len(audit("gate_policy_changed")) == 1 and usage(genv)["exhausted"]
    page = genv["alex_ui"].get(f'/data/{genv["customers"]}').text
    assert 'name="query_limit"' in page
    assert 'name="query_limit"' not in genv["bob_ui"].get(f'/data/{genv["customers"]}').text


def test_rollback_refunds_release_and_retry_is_not_double_charged(genv):
    configure(genv, query_limit="1")
    nr, result = run(genv)
    with db.session() as s:
        actor = s.scalar(select(Actor).where(Actor.name == "alex"))
        output_gate.decide(s, result.id, actor, "release")
        s.flush()
        s.rollback()
    assert usage(genv)["released_count"] == 0 and latest(nr).status == "pending_approval"
    assert "err=" not in decide(genv["alex_ui"], result.id, "release").headers["location"]
    assert "err=" in decide(genv["alex_ui"], result.id, "release").headers["location"]
    assert usage(genv)["released_count"] == 1
    # Even deletion of the result cannot replenish the dataset's counter.
    with db.session() as s:
        s.delete(s.get(RunResult, result.id))
        s.commit()
    assert usage(genv)["released_count"] == 1


@pytest.mark.parametrize("noise", [False, True])
def test_concurrent_releases_cannot_overspend_dataset_budget(genv, noise):
    fields = {"epsilon": "0.1", "sensitivity": "2", "epsilon_limit": "0.1"} if noise else {"query_limit": "1"}
    configure(genv, **fields)
    _, a = run(genv)
    _, b = run(genv)
    token, barrier = usage(genv)["token"], Barrier(2)

    def release(result_id):
        with db.session() as s:
            actor = s.scalar(select(Actor).where(Actor.name == "alex"))
            barrier.wait(timeout=10)
            try:
                output_gate.decide(s, result_id, actor, "release", noise_reviewed=noise, policy_token=token)
                s.commit()
                return "released"
            except Conflict as exc:
                s.rollback()
                return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(release, [a.id, b.id]))
    assert sorted(outcomes) == ["gate_budget_exhausted", "released"]
    assert usage(genv)["released_count"] == 1
    assert Decimal(usage(genv)["epsilon_spent"]) == (Decimal("0.1") if noise else 0)
