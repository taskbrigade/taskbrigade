"""The eight stage-1 workflow MCP tools. Server authorization remains authoritative."""
from cli.client import ApiError, explain
from cli.i18n import _
from cli.workflows import list_run_params, report, workflow_path


class WorkflowTools:
    def _workflow_request(self, method, path, **kwargs):
        try:
            return report(self.client.request(method, path, **kwargs).json())
        except (ApiError, ValueError) as exc:
            return _("Failed: %(error)s", error=explain(exc, "workflow") if isinstance(exc, ApiError) else exc)

    def list_workflows(self, project: str | None = None) -> str:
        return self._workflow_request("GET", "/workflows", params={"project": project} if project else {})

    def show_workflow(self, workflow: str, version: int | None = None) -> str:
        try:
            path = workflow_path(workflow)
        except ValueError as exc:
            return _("Failed: %(error)s", error=exc)
        return self._workflow_request("GET", path, params={"version": version} if version else {})

    def start_workflow_run(self, workflow: str, params: dict, budget_tokens: int | None = None,
                           budget_cents: int | None = None, dry_run: bool = False) -> str:
        try:
            path = workflow_path(workflow)
        except ValueError as exc:
            return _("Failed: %(error)s", error=exc)
        body = {"params": params, "dry_run": dry_run}
        if budget_tokens is not None:
            body["budget_tokens"] = budget_tokens
        if budget_cents is not None:
            body["budget_cents"] = budget_cents
        return self._workflow_request("POST", path + "/runs", json=body)

    def list_workflow_runs(self, workflow: str | None = None, project: str | None = None,
                           status: str | None = None, limit: int = 20) -> str:
        try:
            params = list_run_params(workflow, project, status, limit)
        except ValueError as exc:
            return _("Failed: %(error)s", error=exc)
        return self._workflow_request("GET", "/workflow-runs", params=params)

    def show_workflow_run(self, run_id: int) -> str:
        return self._workflow_request("GET", f"/workflow-runs/{run_id}")

    def pause_workflow_run(self, run_id: int, reason: str) -> str:
        return self._workflow_request("POST", f"/workflow-runs/{run_id}/pause", json={"reason": reason})

    def resume_workflow_run(self, run_id: int, from_step: str | None = None, skip_failed: bool = False) -> str:
        return self._workflow_request("POST", f"/workflow-runs/{run_id}/resume",
                                      json={"from_step": from_step, "skip_failed": skip_failed})

    def cancel_workflow_run(self, run_id: int, reason: str) -> str:
        return self._workflow_request("POST", f"/workflow-runs/{run_id}/cancel", json={"reason": reason})

    def draft_workflow(self, workflow: str, yaml_document: str) -> str:
        """Draft a template version from a canonical YAML document; never publishes (a human publishes)."""
        try:
            path = workflow_path(workflow)
        except ValueError as exc:
            return _("Failed: %(error)s", error=exc)
        try:
            return report(self.client.request(
                "PUT", path + "/import", content=yaml_document,
                headers={"Content-Type": "application/yaml"}).json())
        except ApiError as exc:
            return _("Failed: %(error)s", error=explain(exc, "workflow"))
        except ValueError as exc:
            return _("Failed: %(error)s", error=exc)


def register(server, app):
    @server.tool(description=_("List workflows visible to you, including parameters, current version and last run status."))
    def list_workflows(project: str | None = None) -> str:
        return app.list_workflows(project)

    @server.tool(description=_("Show a workflow (PROJ/key), its steps, roles, classes and parameters. Optionally select a version."))
    def show_workflow(workflow: str, version: int | None = None) -> str:
        return app.show_workflow(workflow, version)

    @server.tool(description=_("Start a workflow (PROJ/key) with parameter values and optional budgets. Orchestrator agents must be project members. dry_run previews the first step and checks roles without creating tasks or runs."))
    def start_workflow_run(workflow: str, params: dict, budget_tokens: int | None = None,
                           budget_cents: int | None = None, dry_run: bool = False) -> str:
        return app.start_workflow_run(workflow, params, budget_tokens, budget_cents, dry_run)

    @server.tool(description=_("List workflow runs by workflow (PROJ/key), project or status. limit defaults to 20 (maximum 200)."))
    def list_workflow_runs(workflow: str | None = None, project: str | None = None,
                           status: str | None = None, limit: int = 20) -> str:
        return app.list_workflow_runs(workflow, project, status, limit)

    @server.tool(description=_("Show a workflow run, its step attempts, tasks, outputs, budget and pause reason. Restricted outputs are masked."))
    def show_workflow_run(run_id: int) -> str:
        return app.show_workflow_run(run_id)

    @server.tool(description=_("Pause a workflow run with a reason. Active tasks continue; no successor starts until resume."))
    def pause_workflow_run(run_id: int, reason: str) -> str:
        return app.pause_workflow_run(run_id, reason)

    @server.tool(description=_("Resume a paused run. from_step restarts the current or an earlier step; skip_failed skips only optional steps, never approvals or gates."))
    def resume_workflow_run(run_id: int, from_step: str | None = None, skip_failed: bool = False) -> str:
        return app.resume_workflow_run(run_id, from_step, skip_failed)

    @server.tool(description=_("Cancel a workflow run with a reason and cancel its open tasks. Completed tasks remain in history."))
    def cancel_workflow_run(run_id: int, reason: str) -> str:
        return app.cancel_workflow_run(run_id, reason)

    @server.tool(description=_("Draft a workflow version (PROJ/key) from a canonical YAML document (export of a version or hand-written). "
                               "Creates or replaces the unpublished draft only – a human publishes it in the web UI. The document is validated "
                               "with the same publish checks as the editor; blocking problems are refused. Template key is PROJ/key."))
    def draft_workflow(workflow: str, yaml_document: str) -> str:
        return app.draft_workflow(workflow, yaml_document)
