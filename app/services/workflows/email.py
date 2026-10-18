"""Email notify steps file ordinary, auditable result-mail requests on the preceding task."""
import json

from app.errors import Unprocessable
from app.i18n import _
from app.models import Actor
from app.services import result_mail
from app.services.workflows.contracts import parse_output_contract
from app.services.workflows.placeholders import Placeholder, PlaceholderError, classify_path, parse_template, render
from app.services.workflows.rendering import step_context


def enqueue(session, run, row, step, config, previous):
    if not previous:
        raise Unprocessable(_("An email notify step requires an earlier task."), code="workflow_config_invalid")
    task = previous[-1].task
    ctx = step_context(session, run)
    cls = result_mail.mail_class(run.project, task, step.data_class)

    def restricted(trace):
        path = classify_path(trace["path"])
        origin = next((r for r in previous if r.step.step_key == path.step), None)
        masked = trace["masked"] or (origin is not None and result_mail.mail_class(
            run.project, origin.task, origin.output_class or origin.step.data_class) != "cloud_ok")
        return masked and not (path.kind == "step_builtin" and path.builtin in ("task_nr", "status"))

    def text(value):
        if not isinstance(value, str):
            raise Unprocessable(_("Invalid workflow step configuration"), code="workflow_config_invalid")
        resolved = render(value, ctx)
        # Released references are allowed as attachments, never interpolate restricted task contents into mail.
        if any(restricted(t) for t in resolved.trace):
            raise Unprocessable(_("Restricted workflow contents cannot be inserted into email text."),
                                code="mail_local_only")
        return resolved.text

    def addresses(key):
        values = config.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise Unprocessable(_("Invalid workflow step configuration"), code="workflow_config_invalid")
        out = []
        for value in values:
            if value == "owner":
                owner = session.get(Actor, run.project.owner_id)
                value = owner.email if owner else ""
            out.append(text(value))
        return out

    refs = config.get("attachments", [])
    if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
        raise Unprocessable(_("Invalid workflow email attachment."), code="mail_attachment_invalid")
    attachments = []
    try:
        for ref in refs:
            parts = parse_template(ref)
            if len(parts) == 1 and isinstance(parts[0], Placeholder):
                part = parts[0]
                path = classify_path(part.path)
                origin = next((r for r in previous if r.step.step_key == path.step), None)
                if origin is None or part.filters or path.fields:
                    raise Unprocessable(_("Invalid workflow email attachment."), code="mail_attachment_invalid")
                render(ref, ctx)  # Checks completion and presence before reading the typed value.
                outputs = json.loads(origin.output_json or "{}")
                value = outputs.get("output", {}).get(path.output) if path.kind == "step_output" else outputs.get("result")
                spec = parse_output_contract(origin.step.outputs_json).get(path.output)
                if spec and spec.type == "file" and isinstance(value, dict) and "path" in value:
                    ref = f"output:{origin.id}:{path.output}"
                elif ((spec and spec.type == "result") or (path.kind == "step_builtin" and path.builtin == "result")) and (
                    isinstance(value, int) and not isinstance(value, bool) or isinstance(value, str) and value.isdigit()
                ):
                    ref = f"result:{value}"
                else:
                    raise Unprocessable(_("Invalid workflow email attachment."), code="mail_attachment_invalid")
            elif "{{" in ref:
                raise Unprocessable(_("Invalid workflow email attachment."), code="mail_attachment_invalid")
            attachments.append(ref)
        mail = result_mail.request(
            session, run.template.owner, task, to=addresses("to"), cc=addresses("cc"),
            subject=text(config.get("subject", step.title_tpl)),
            body=text(config.get("body", config.get("text", step.description_tpl or step.title_tpl))),
            attachments=attachments, data_class=cls, via="workflow")
    except PlaceholderError as exc:
        raise Unprocessable(_("Workflow placeholders could not be resolved"), code=exc.code) from exc
    row.output_json = json.dumps({"mail_id": mail.id})
