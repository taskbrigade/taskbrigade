"""Server-side steps of deploy/docker/data-zones-test.sh, piped into the app container (`python - <step>`).

`setup`: data store on, project DZT (owner admin), runner agent `runner-dzt` with a join code – prints
`SETUP {json}`. `assign`: hand the open profile tasks of DZT to the runner (claims only take assigned tasks).
`status`: datasets, their tasks and the stored profiles of DZT – prints `STATUS {json}`. Throwaway stacks only.
"""
import json
import sys

from sqlalchemy import select

from app.config import get_settings
from app.db import db
from app.models import Actor, Dataset, DatasetProfile, Project, Task
from app.services import actors, onboarding, projects, settings_store

PROJECT, RUNNER = "DZT", "runner-dzt"


def _admin(session) -> Actor:
    return session.scalar(select(Actor).where(Actor.name == "admin"))


def setup(session) -> dict:
    admin = _admin(session)
    settings_store.update(session, {"data_enabled": "true"}, admin.id)
    project = session.scalar(select(Project).where(Project.project_key == PROJECT))
    if project is None:
        # Created as an idea (active needs a Gitea repository), then active: claims only take active projects.
        project, _setup = projects.create(session, admin, key=PROJECT, name="Data zones test", privacy="local_only",
                                          status="idea")
        project.status = "active"
    runner = session.scalar(select(Actor).where(Actor.name == RUNNER))
    if runner is None:
        runner = actors.create(session, name=RUNNER, display_name="Runner (Docker test)", actor_type="agent",
                               agent_kind="runner", issue_token=False, owner=admin).actor
    code, _expires = onboarding.create_code(session, runner, admin)
    return {"code": code, "project": PROJECT, "runner": RUNNER}


def assign(session) -> dict:
    runner = session.scalar(select(Actor).where(Actor.name == RUNNER))
    project = session.scalar(select(Project).where(Project.project_key == PROJECT))
    tasks = list(session.scalars(select(Task).where(Task.project_id == project.id, Task.task_kind == "profile",
                                                    Task.assigned_to.is_(None), Task.status == "defined")))
    for task in tasks:
        task.assigned_to = runner.id
    return {"assigned": [t.task_nr for t in tasks]}


def status(session) -> dict:
    project = session.scalar(select(Project).where(Project.project_key == PROJECT))
    rows = []
    for ds in session.scalars(select(Dataset).where(Dataset.project_id == project.id).order_by(Dataset.id)):
        profile = session.scalar(select(DatasetProfile).where(DatasetProfile.dataset_id == ds.id)
                                 .order_by(DatasetProfile.id.desc()).limit(1))
        rows.append({"dataset": ds.dataset_key, "status": ds.status, "channel": ds.upload_channel,
                     "profile": profile.status if profile else None})
    tasks = [{"task": t.task_nr, "kind": t.task_kind, "status": t.status}
             for t in session.scalars(select(Task).where(Task.project_id == project.id, Task.task_kind == "profile")
                                      .order_by(Task.id))]
    return {"datasets": rows, "tasks": tasks}


def main(step: str) -> int:
    db.init_from_settings(get_settings())
    with db.session() as session:
        result = {"setup": setup, "assign": assign, "status": status}[step](session)
        session.commit()
    print(f"{step.upper()} {json.dumps(result)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
