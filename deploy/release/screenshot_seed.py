"""Neutral demo data for the README screenshots (`deploy/release/screenshots.py`).

Runs inside the app container of a fresh `docker compose --profile gitea` stack, after `gitea-init` created the
agent `demo-agent`, the project `DEMO` and its task DEMO-0001:

    docker compose exec -T taskbrigade python - < deploy/release/screenshot_seed.py

Everything goes through the app's services (state machine, sprints, Gitea client), so statuses, threads, pull
requests and merges are real. Branches are created with the Gitea contents API instead of `git push`. Names and
texts are invented examples only – never real people, hosts or projects. Not idempotent: run it once per stack.
"""
import base64
import json
import time
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.container import database_url
from app.db import make_engine
from app.models import Actor, Project
from app.services import actors, members, onboarding, projects, settings_store, sprints, tasks
from app.services.state_machine import transition

ADMIN = "admin"
DEMO = "DEMO"

HUMANS = [("alex", "Alex", "member")]
# name, display name, kind, role, notes
AGENTS = [
    ("claude-code", "Claude Code", "claude_code", "worker", "Runner agent on the build machine"),
    ("codex", "Codex", "codex", "worker", "Runner agent on the build machine"),
    ("opencode", "OpenCode", "opencode", "worker", "Runner agent with a local model"),
    ("research-agent", "Research agent", "hermes", "worker", "Hermes agent for research and documentation"),
    ("openclaw-agent", "OpenClaw agent", "openclaw", "orchestrator", "Plans tasks from the team chat"),
]


def actor(s: Session, name: str) -> Actor:
    return s.scalar(select(Actor).where(Actor.name == name))


def branch_with_file(s: Session, project: Project, branch: str, path: str, content: str, message: str) -> str:
    """Create `branch` from the default branch with one new file; return the commit SHA."""
    client = settings_store.gitea_client(s)
    body = {"branch": project.default_branch, "new_branch": branch, "message": message,
            "files": [{"operation": "create", "path": path, "content": base64.b64encode(content.encode()).decode()}]}
    result = client._json("POST", f"{client._repo_path(project.gitea_owner, project.gitea_repo)}/contents",
                          json=body) or {}
    return result["commit"]["sha"]


def submit(s: Session, task, agent: Actor, path: str, content: str, summary: str, open_points=None) -> None:
    project = projects.get(s, DEMO)
    sha = branch_with_file(s, project, task.branch_name, path, content, f"[{task.task_nr}] {task.title}")
    transition(s, task, "submit", agent, {"branch_name": task.branch_name, "commit_sha": sha, "summary": summary,
                                          "open_points": open_points or []})


def wait_mergeable(s: Session, task, timeout: float = 60) -> None:
    """Gitea answers a merge with 405 "Please try again later" while it still checks the new pull request."""
    project = projects.get(s, DEMO)
    client = settings_store.gitea_client(s)
    path = f"{client._repo_path(project.gitea_owner, project.gitea_repo)}/pulls/{task.pr_number}"
    deadline = time.monotonic() + timeout
    while not (client._json("GET", path) or {}).get("mergeable"):
        if time.monotonic() > deadline:
            raise SystemExit(f"Pull request of {task.task_nr} is not mergeable")
        time.sleep(1)


def new(s: Session, owner: Actor, title: str, agent: str | None, description: str, criteria: str, prio: int = 3,
        sprint: int | None = None, **kw):
    return tasks.create(s, owner, project=DEMO, title=title, description=description, acceptance_criteria=criteria,
                        assigned_to=agent, prio=prio, sprint=sprint, **kw)


def seed(s: Session) -> dict:
    """Create the demo data; return the page paths that depend on generated ids."""
    admin = actor(s, ADMIN)
    admin.display_name = "Admin"
    for name, display, role in HUMANS:
        if actor(s, name) is None:
            actors.create(s, name=name, display_name=display, actor_type="human", role=role)
    for name, display, kind, role, notes in AGENTS:
        if actor(s, name) is None:
            agent = actors.create(s, name=name, display_name=display, actor_type="agent", agent_kind=kind,
                                  role=role, runtime="local", notes=notes,
                                  model_location="local" if kind == "opencode" else "cloud").actor
            onboarding.provision_gitea(s, agent)
    claude, codex, research, opencode = (actor(s, n) for n in ("claude-code", "codex", "research-agent", "opencode"))
    demo_agent = actor(s, "demo-agent")
    claude.model_default = "standard-model"
    claude.models_allowed = json.dumps(["standard-model", "large-model"])
    claude.model_tiers = json.dumps({"simple": "standard-model", "standard": "standard-model",
                                     "complex": "large-model"})
    for a in (claude, codex, research, opencode, demo_agent):
        actors.touch_last_seen(s, a)

    project = projects.get(s, DEMO)
    project.name = "Demo shop"
    project.privacy = "cloud_ok"  # Cloud-model agents work on it (`local_only` needs a local model).
    project.description = "Example web shop used to try TaskBrigade"
    today = date.today()
    sprint1 = sprints.create(s, admin, project, name="Sprint 1", goal="Checkout works end to end",
                             start_date=today - timedelta(days=4), end_date=today + timedelta(days=10))

    # DEMO-0001 from gitea-init: question → answer → second claim → pull request in review.
    hello = tasks.get(s, f"{DEMO}-0001")
    sprints.assign(s, hello, sprint1, admin)
    hello.prio = 2

    t_cart = new(s, admin, "Show the cart total including VAT", "claude-code",
                 "The cart page shows the net total only. Show net, VAT and gross total below the item list.",
                 "- Net, VAT (19 %) and gross total are shown\n- Totals are rounded to cents\n- Unit test for "
                 "the calculation", prio=2, sprint=sprint1.id, model_tier="standard")
    t_login = new(s, admin, "Rate-limit failed logins", "codex",
                  "Lock an account for 5 minutes after 10 failed logins within 15 minutes.",
                  "- Limit and lock time are configurable\n- Successful login resets the counter\n- Tests cover "
                  "the lock and the reset", prio=1, sprint=sprint1.id)
    t_mail = new(s, admin, "Send an order confirmation e-mail", "claude-code",
                 "Send a plain-text confirmation to the customer after checkout.",
                 "- E-mail contains order number, items and total\n- SMTP settings come from the environment\n"
                 "- Failed delivery is logged, the order is kept", prio=2, sprint=sprint1.id)
    t_search = new(s, admin, "Product search with filters", "codex",
                   "Add a search field with filters for category and price range.",
                   "- Search matches name and description\n- Filters can be combined\n- Empty result shows a hint",
                   prio=3, sprint=sprint1.id)
    t_docs = new(s, admin, "Document the REST endpoints", "research-agent",
                 "Write `docs/api.md` with every endpoint, its parameters and an example request.",
                 "- Every public endpoint is listed\n- Examples use the demo data", prio=3, sprint=sprint1.id)
    t_ci = new(s, admin, "Add a CI workflow", "opencode",
               "Run lint and tests on every push.", "- Workflow runs ruff and pytest\n- Status badge in the README",
               prio=3, sprint=sprint1.id)
    t_img = new(s, admin, "Resize product images on upload", "opencode",
                "Uploaded product images are stored in full size. Store a 1200 px and a 300 px version.",
                "- Both sizes are generated on upload\n- Originals larger than 10 MB are refused", prio=4,
                sprint=sprint1.id)
    t_coupon = new(s, admin, "Coupon codes at checkout", "claude-code",
                   "Customers can enter one coupon code per order (percentage or fixed amount).",
                   "- Invalid and expired codes show a message\n- Discount appears in the totals", prio=3,
                   sprint=sprint1.id)
    sprints.release(s, admin, sprint1)

    # Sprint 2 stays planned: its drafts fill the sprint editor.
    sprint2 = sprints.create(s, admin, project, name="Sprint 2", goal="Customer accounts",
                             start_date=today + timedelta(days=11), end_date=today + timedelta(days=24))
    new(s, admin, "Customer registration with e-mail confirmation", "claude-code",
        "Customers register with e-mail and password and confirm the address with a link.",
        "- Confirmation link expires after 24 hours\n- Unconfirmed accounts cannot order", prio=2, sprint=sprint2.id,
        model_tier="complex")
    new(s, admin, "Order history page", "codex", "List the customer's orders with status and total.",
        "- Newest order first\n- Link to each order's details", prio=3, sprint=sprint2.id)
    new(s, admin, "Password reset", "codex", "Reset link by e-mail, valid for one hour.", "", prio=3,
        sprint=sprint2.id)
    new(s, admin, "Saved delivery addresses", None, "", "", prio=4, sprint=sprint2.id)

    # Backlog outside the sprints.
    new(s, admin, "Evaluate a payment provider", "research-agent",
        "Compare two payment providers for fees, API quality and test mode.", "- Short comparison table in the PR",
        prio=4)
    tasks.create(s, admin, project=DEMO, title="Dark mode for the shop front", prio=5, status="draft")

    # Done: two merged pull requests.
    for task, agent, path, content, summary in (
            (t_login, codex, "auth/limits.py", "MAX_FAILED = 10\nLOCK_MINUTES = 5\nWINDOW_MINUTES = 15\n",
             "Failed logins are counted per account; after 10 within 15 minutes the account is locked for "
             "5 minutes. Tests: 6 new, all green."),
            (t_ci, opencode, ".ci/workflow.yml", "on: push\njobs:\n  test:\n    steps:\n      - run: ruff check .\n"
             "      - run: pytest -q\n", "CI runs ruff and pytest on every push; badge added to the README.")):
        transition(s, task, "claim", agent)
        submit(s, task, agent, path, content, summary)
        wait_mergeable(s, task)
        transition(s, task, "approve", admin, {"body": "Looks good, thanks."})

    # In review with a thread: question, answer, pull request.
    transition(s, hello, "claim", demo_agent)
    transition(s, hello, "ask", demo_agent, {"body": "Should `hello.py` also accept a name as an argument, "
                                                     "e.g. `python hello.py Alex`?"})
    transition(s, hello, "answer", admin, {"body": "Yes, optional: without an argument keep the default text."})
    transition(s, hello, "claim", demo_agent)
    tasks.comment(s, demo_agent, hello, "Started with the optional argument; adding a test for both cases.")
    submit(s, hello, demo_agent, "hello.py",
           'import sys\n\nname = sys.argv[1] if len(sys.argv) > 1 else "TaskBrigade"\nprint(f"Hello from {name}!")\n',
           "Added `hello.py`: prints `Hello from TaskBrigade!`, or greets the name given as the first argument. "
           "Checked with `python hello.py` and `python hello.py Alex`.",
           ["Add a test runner to the repository so the script can be covered by CI."])

    transition(s, t_cart, "claim", claude)
    submit(s, t_cart, claude, "shop/totals.py", "VAT_RATE = 0.19\n",
           "Cart shows net, VAT and gross total; amounts rounded half-up to cents. Unit tests for the calculation.")

    # Changes requested.
    transition(s, t_search, "claim", codex)
    submit(s, t_search, codex, "shop/search.py", "FIELDS = ('name', 'description')\n",
           "Search over name and description with category and price filters.")
    transition(s, t_search, "reject", admin, {"body": "The price filter ignores the upper bound – please fix and "
                                                      "add a test for it."})

    # Waiting for an answer, in progress, aborted.
    transition(s, t_mail, "claim", claude)
    transition(s, t_mail, "ask", claude, {"body": "Which sender address should the confirmation use? "
                                                  "`shop@example.com` or a configurable one?"})
    transition(s, t_docs, "claim", research)
    transition(s, t_coupon, "claim", claude)
    transition(s, t_img, "claim", opencode)
    transition(s, t_img, "abort", opencode, {"body": "The image library needs a system package that is missing "
                                                      "on this machine; please assign the task to another agent."})
    run = seed_workflows(s, admin, project, research)
    seed_email(s, admin)
    return {"editor": f"/projects/{DEMO}/sprints/{sprint2.id}/editor", "run": f"/workflow-runs/{run.id}"}


# The minimal example of the README (section Workflows): it has to pass the publish checks.
RELEASE_NOTES_YAML = """\
name: Release notes
description: An agent drafts the release notes, a human approves them, the owner is notified.
params:
  - {name: version, type: string, required: true, label: Version}
steps:
  - key: draft
    type: agent
    assignee: research-agent
    title: "Release notes for {{params.version}}"
    description: Summarise the merged changes since the last release in docs/release-notes.md.
    acceptance: Every merged pull request is listed once.
  - key: approve
    type: approval
    reviewer: admin
    title: "Approve the release notes for {{params.version}}"
  - key: announce
    type: notify
    title: "Release notes for {{params.version}} are ready: {{steps.draft.pr}}"
    config: {to: [owner]}
"""

KPI_REPORT_YAML = """\
name: Weekly KPI report
description: Every Monday an agent collects the shop figures and writes the weekly report.
steps:
  - key: report
    type: agent
    assignee: codex
    title: Weekly KPI report
    description: Collect orders, revenue and returns of the last week and write reports/weekly.md.
    acceptance: The report has a table per figure and a short summary.
  - key: send
    type: notify
    title: "Weekly KPI report is ready: {{steps.report.pr}}"
"""


def seed_workflows(s: Session, admin: Actor, project: Project, research: Actor):
    """Two published templates, a schedule and one run whose first step is merged and whose approval waits."""
    from app.services.workflows import api, engine, schedules, transfer

    settings_store.update(s, {"workflows_enabled": "true"}, admin.id)
    for name in (ADMIN, "research-agent", "codex"):
        members.ensure_member(s, project, actor(s, name))
    # Its open tasks (also defined ones) count towards the parallel limit; a step is assigned only below it.
    research.max_parallel_tasks = 3
    templates = {}
    for key, document in (("release-notes", RELEASE_NOTES_YAML), ("kpi-report", KPI_REPORT_YAML)):
        template, _draft, _warnings = api.import_draft(s, admin, project, key,
                                                         transfer.parse_yaml(document))
        api.publish(s, admin, template)
        templates[key] = template
    schedules.save(s, admin, templates["kpi-report"], {"name": "Mondays 07:00", "cron_expr": "0 7 * * mon",
                                                        "enabled": True})
    run = engine.start(s, admin, templates["release-notes"], params={"version": "1.4.0"})
    s.flush()
    if not run_tasks(s, run):
        raise SystemExit(f"Workflow run {run.id} created no task: {run.status} {run.pause_reason}")
    step = run_tasks(s, run)[0]
    transition(s, step, "claim", research)
    submit(s, step, research, "docs/release-notes.md",
           "# Release notes 1.4.0\n\n- Cart total including VAT\n- Rate-limited logins\n- CI workflow\n",
           "Release notes for 1.4.0 in docs/release-notes.md: three merged pull requests, one line each.")
    wait_mergeable(s, step)
    transition(s, step, "approve", admin, {"body": "Thanks, approved."})
    return run


def run_tasks(s: Session, run) -> list:
    from app.models import Task, WorkflowRunStep

    return list(s.scalars(select(Task).join(WorkflowRunStep, WorkflowRunStep.task_id == Task.id)
                          .where(WorkflowRunStep.run_id == run.id).order_by(WorkflowRunStep.position)))


def seed_email(s: Session, admin: Actor) -> None:
    """Email channel with a placeholder SMTP server (nothing is sent: no event follows) and a confirmed address."""
    settings_store.update(s, {"smtp_host": "smtp.example.com", "smtp_port": "587", "smtp_security": "starttls",
                              "mail_from_address": "taskbrigade@example.com", "mail_from_name": "TaskBrigade",
                              "email_enabled": "true"}, admin.id)
    admin.email = "admin@example.com"
    admin.email_verified_at = datetime.now(UTC).replace(tzinfo=None)


def main() -> None:
    engine = make_engine(database_url(get_settings()))
    with Session(engine) as s:
        paths = seed(s)
        s.commit()
    print("SEED " + json.dumps(paths))  # Read by screenshots.py.


main()
