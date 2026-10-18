"""Start templates of the Workflows page: example definitions that prefill the new-workflow form.

A starter is plain data for `api.Draft` (steps with role-based assignment, so it needs no concrete actor). It adds no
engine feature: the schedule or trigger a starter suggests is only a hint (`next_step`) for the Schedules / Event
triggers tab after the first publication. Only agent, approval and notify steps are used, so every starter passes the
publish checks without data zones.
"""
from app.i18n import N_

STARTERS = {
    "chain": {
        "name": N_("Task chain"), "badge": N_("3 steps"), "key": "task_chain",
        "text": N_("Run tasks one after another – e.g. an agent writes a draft, a reviewer checks it, then a "
                   "notification goes out."),
        "next_step": None,
        "steps": [
            {"step_key": "draft", "step_type": "agent", "agent_role": "writer", "title_tpl": "Write the draft",
             "description_tpl": "Write the draft and attach the result.", "acceptance_tpl": "The draft is complete."},
            {"step_key": "review", "step_type": "approval", "agent_role": "reviewer",
             "title_tpl": "Review the draft", "description_tpl": "Check the draft against the acceptance criteria.",
             "acceptance_tpl": "The draft is approved or sent back."},
            {"step_key": "done", "step_type": "notify", "title_tpl": "Chain finished"},
        ],
    },
    "schedule": {
        "name": N_("Scheduled report"), "badge": N_("Schedule"), "key": "scheduled_report",
        "text": N_("Run a task on a schedule (cron) and send the result by email or Telegram."),
        "next_step": N_("After publishing, add a schedule (for example “0 7 * * mon-fri”) under Schedules."),
        "steps": [
            {"step_key": "report", "step_type": "agent", "agent_role": "analyst", "title_tpl": "Create the report",
             "description_tpl": "Collect the figures and write the report.",
             "acceptance_tpl": "The report is attached and readable."},
            {"step_key": "send", "step_type": "notify", "title_tpl": "Report is ready"},
        ],
    },
    "event": {
        "name": N_("Event-triggered"), "badge": N_("Trigger"), "key": "event_triggered",
        "text": N_("Start when something happens: dataset_ready, workflow_succeeded, a task done."),
        "next_step": N_("After publishing, add an event trigger (for example workflow_succeeded) under Event "
                        "triggers."),
        "steps": [
            {"step_key": "handle", "step_type": "agent", "agent_role": "worker", "title_tpl": "Handle the event",
             "description_tpl": "Process what the event announced.", "acceptance_tpl": "The event is handled."},
            {"step_key": "done", "step_type": "notify", "title_tpl": "Event handled"},
        ],
    },
}


def get(starter: str | None) -> dict | None:
    return STARTERS.get(starter or "")


def draft_body(starter: dict, name: str | None = None) -> dict:
    """Keyword arguments for `api.Draft`; copies the steps so a caller cannot change the shared example."""
    return {"name": name or None, "steps": [dict(step) for step in starter["steps"]]}
