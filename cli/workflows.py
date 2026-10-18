"""Workflow client helpers and the `tbr workflow` command group."""
import json
import re
import sys

import typer

from cli.i18n import _


def workflow_path(workflow):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*/[A-Za-z0-9_-]{1,40}", workflow):
        raise ValueError(_("Workflow must be PROJ/key"))
    project, key = workflow.split("/", 1)
    return f"/workflows/{project.upper()}/{key}"


def run_line(run):
    return (_("Run #%(seq)s (id %(id)s) %(workflow)s: %(status)s %(position)s/%(total)s",
              seq=run["seq"], id=run["id"], workflow=run["workflow"], status=run["status"],
              position=run.get("current_position") or 0, total=run.get("total_steps") or 0)
            + (f" – {run['current_task']} {run.get('current_task_status') or ''}" if run.get("current_task") else ""))


def report(data):
    if isinstance(data, list):
        return "\n".join(report(item) for item in data) or _("No workflows or runs found.")
    if "seq" in data:
        head = run_line(data)
    elif data.get("dry_run"):
        head = _("Workflow preview (nothing created): %(workflow)s", workflow=data["workflow"])
    else:
        head = _("Workflow %(workflow)s: %(name)s (version %(version)s)", workflow=data["workflow"],
                 name=data["name"], version=data.get("version") or "–")
    return head + "\n" + json.dumps(data, indent=2, ensure_ascii=False, default=str)


def list_run_params(workflow=None, project=None, status=None, limit=20):
    if workflow:
        workflow_path(workflow)
    return {k: v for k, v in {"workflow": workflow, "project": project, "status": status, "limit": limit}.items()
            if v is not None}


def register(app):
    group = typer.Typer(help=_("Inspect workflows and start or control runs."), no_args_is_help=True)
    app.add_typer(group, name="workflow")

    def call(method, path, as_json=False, **kwargs):
        from cli.tm import _call, _dump

        data = _call(method, path, **kwargs)
        if as_json:
            _dump(data)
        else:
            typer.echo(report(data))

    def path(workflow):
        try:
            return workflow_path(workflow)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc

    @app.command("workflows", help=_("List available workflow templates."))
    def workflows(project: str = typer.Option(None, "--project", "-p"),
                  as_json: bool = typer.Option(False, "--json")):
        call("GET", "/workflows", as_json, params={"project": project} if project else {})

    @group.command("show", help=_("Show a workflow and its parameters and steps."))
    def show(workflow: str, version: int = typer.Option(None, min=1),
             as_json: bool = typer.Option(False, "--json")):
        call("GET", path(workflow), as_json, params={"version": version} if version else {})

    @group.command("start", help=_("Start a workflow run or preview its first step without creating anything."))
    def start(workflow: str, param: list[str] = typer.Option(None, "--param"),
              budget_tokens: int = typer.Option(None, min=0), budget_cents: int = typer.Option(None, min=0),
              queue: bool = typer.Option(False, "--queue"),
              force_parallel: bool = typer.Option(False, "--force-parallel"),
              dry_run: bool = typer.Option(False, "--dry-run"), as_json: bool = typer.Option(False, "--json")):
        params = {}
        for item in param or []:
            key, sep, value = item.partition("=")
            if not sep or not key or key in params:
                raise typer.BadParameter(_("Use each --param once as name=value"))
            params[key] = value
        body = {"params": params, "dry_run": dry_run}
        if queue:
            body["queue"] = True
        if force_parallel:
            body["force_parallel"] = True
        if budget_tokens is not None:
            body["budget_tokens"] = budget_tokens
        if budget_cents is not None:
            body["budget_cents"] = budget_cents
        call("POST", path(workflow) + "/runs", as_json, json=body)

    @group.command("runs", help=_("List workflow runs, optionally filtered by workflow and status."))
    def runs(workflow: str = typer.Argument(None), project: str = typer.Option(None, "--project", "-p"),
             status: str = typer.Option(None), limit: int = typer.Option(20, min=1, max=200),
             as_json: bool = typer.Option(False, "--json")):
        if workflow:
            path(workflow)
        call("GET", "/workflow-runs", as_json, params=list_run_params(workflow, project, status, limit))

    @group.command("run", help=_("Show a workflow run with tasks, outputs and budget."))
    def run(run_id: int, as_json: bool = typer.Option(False, "--json")):
        call("GET", f"/workflow-runs/{run_id}", as_json)

    @group.command("pause", help=_("Pause a workflow run; active tasks keep running."))
    def pause(run_id: int, reason: str = typer.Option(None), as_json: bool = typer.Option(False, "--json")):
        call("POST", f"/workflow-runs/{run_id}/pause", as_json, json={"reason": reason})

    @group.command("resume", help=_("Resume a paused workflow, retry a step or skip an optional failed step."))
    def resume(run_id: int, from_step: str = typer.Option(None), skip: bool = typer.Option(False, "--skip"),
               reason: str = typer.Option(None), as_json: bool = typer.Option(False, "--json")):
        call("POST", f"/workflow-runs/{run_id}/resume", as_json,
             json={"reason": reason, "from_step": from_step, "skip_failed": skip})

    @group.command("cancel", help=_("Cancel a workflow run and its open tasks."))
    def cancel(run_id: int, reason: str = typer.Option(None), as_json: bool = typer.Option(False, "--json")):
        call("POST", f"/workflow-runs/{run_id}/cancel", as_json, json={"reason": reason})

    @group.command("export", help=_("Write a template version (default: current) as canonical YAML."))
    def export(workflow: str, version: int = typer.Option(None, min=1),
               output: str = typer.Option(None, "--output", "-o")):
        from cli.tm import _client

        client = _client()
        data = client.request("GET", path(workflow) + "/export",
                              params={"version": version} if version else {}).text
        if output:
            from pathlib import Path

            Path(output).write_text(data, encoding="utf-8")
            typer.echo(_("Wrote %(path)s", path=output))
        else:
            typer.echo(data, nl=False)

    @group.command("import", help=_("Store a YAML document as an unpublished draft (a human publishes it)."))
    def import_(workflow: str, file: str = typer.Argument(..., help=_("YAML file ('-' for stdin)")),
                as_json: bool = typer.Option(False, "--json")):
        from cli.tm import _client

        if file == "-":
            data = sys.stdin.read()
        else:
            from pathlib import Path

            path_obj = Path(file)
            if not path_obj.is_file():
                raise typer.BadParameter(_("File not found: %(path)s", path=file))
            data = path_obj.read_text(encoding="utf-8")
        client = _client()
        response = client.request("PUT", path(workflow) + "/import", content=data,
                                  headers={"Content-Type": "application/yaml"})
        if as_json:
            from cli.tm import _dump

            _dump(response.json())
        else:
            typer.echo(report(response.json()))
            typer.echo(_("Draft saved. A human publishes it in the web UI."))
