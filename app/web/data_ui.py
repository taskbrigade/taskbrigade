"""Menu "Data": dataset list, upload, detail (research/konzept-datenzonen.md §8, guide docs/data-store.md).

The upload is parsed from the raw request stream instead of `request.form()`: Starlette spools larger files to a
temporary file, and the plaintext must never touch the disk. Each chunk goes straight into the encrypting writer;
only ciphertext is written below `TBR_DATA_DIR/incoming`. Form fields must come before the file (the form puts the
file input last), so the CSRF token and the upload right are checked before the first byte is stored.
"""
import secrets
import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, StreamingResponse
from python_multipart.multipart import MultipartParser, parse_options_header

from app.auth import UiUser, csrf_protect
from app.errors import AppError, Unprocessable
from app.i18n import gettext as _
from app.models import DATASET_CLASSES, Task
from app.services import data_audit, dataset_profiles, datasets, gate_privacy, output_gate
from app.web.common import render, ui_actor, web_session
from app.web.tasks_ui import require_user

router = APIRouter(prefix="/data")
POST = [Depends(csrf_protect)]
FILE_FIELD = "file"
MAX_FIELD_BYTES = 16 * 1024
UPLOAD_SLACK = 1024 * 1024  # Multipart overhead allowed on top of the size limit


def _labels() -> dict:
    return {"status_labels": {k: _(v) for k, v in datasets.STATUS_LABELS.items()},
            "class_labels": {k: _(v) for k, v in datasets.CLASS_LABELS.items()}}


@router.get("")
def listing(request: Request, user: UiUser = Depends(require_user), project: str = "", status: str = "live",
            data_class: str = "", q: str = ""):
    with web_session() as s:
        me = ui_actor(s, user)
        projects = datasets.visible_projects(s, me)
        rows = datasets.search(s, me, project or None, status or None, data_class or None, q)
        keys = {p.id: p.project_key for p in projects}
        problem = datasets.problem(s)
        can_upload = bool(datasets.upload_projects(s, me)) and problem is None
        left = {r.id: datasets.retention_left(r) for r in rows}
        return render(request, "data_list.html", rows=rows, projects=projects, project_keys=keys,
                      f={"project": project, "status": status, "data_class": data_class, "q": q},
                      classes=DATASET_CLASSES, problem=problem, can_upload=can_upload, retention_left=left,
                      warn_days=datasets.WARN_DAYS, enabled=datasets.enabled(s), **_labels())


def _upload_page(request: Request, user: UiUser, status: int = 200, values: dict | None = None, **ctx):
    with web_session() as s:
        me = ui_actor(s, user)
        projects = datasets.upload_projects(s, me)
        replaceable = [d for p in projects for d in datasets.search(s, me, p.project_key, "live")]
        keys = {p.id: p.project_key for p in projects}
        return render(request, "data_upload.html", status, projects=projects, replaceable=replaceable,
                      project_keys=keys, values=values or {}, problem=datasets.problem(s),
                      max_mb=datasets.max_upload_bytes(s) // (1024 * 1024),
                      default_retention=datasets.retention_days(s), classes=DATASET_CLASSES, **_labels(), **ctx)


@router.get("/upload")
def upload_form(request: Request, user: UiUser = Depends(require_user), project: str = ""):
    return _upload_page(request, user, values={"project": project})


class _StreamForm:
    """Callbacks for `python_multipart.MultipartParser`: small fields are collected, the part named `file` is
    handed to the object returned by `on_file(fields, filename, content_type)` chunk by chunk."""

    def __init__(self, on_file):
        self.on_file = on_file
        self.fields: dict[str, str] = {}
        self.file = None
        self._headers: dict[str, str] = {}
        self._field, self._value = bytearray(), bytearray()
        self._name: str | None = None
        self._target = None  # The upload sink while inside the file part
        self._buf = bytearray()

    def callbacks(self) -> dict:
        return {"on_part_begin": self._begin, "on_header_field": self._header_field,
                "on_header_value": self._header_value, "on_header_end": self._header_end,
                "on_headers_finished": self._headers_finished, "on_part_data": self._data,
                "on_part_end": self._end}

    def _begin(self):
        self._headers, self._name, self._target = {}, None, None
        self._buf = bytearray()

    def _header_field(self, data, start, end):
        self._field += data[start:end]

    def _header_value(self, data, start, end):
        self._value += data[start:end]

    def _header_end(self):
        self._headers[bytes(self._field).decode("latin-1").lower()] = bytes(self._value).decode("latin-1")
        self._field, self._value = bytearray(), bytearray()

    def _headers_finished(self):
        _disposition, options = parse_options_header(self._headers.get("content-disposition", ""))
        name = options.get(b"name", b"").decode("utf-8", "replace")
        self._name = name
        if b"filename" in options and name == FILE_FIELD:
            if self.file is not None:
                raise Unprocessable(_("Upload one file at a time"), code="invalid_dataset_field")
            filename = options[b"filename"].decode("utf-8", "replace")
            self.file = self._target = self.on_file(self.fields, filename, self._headers.get("content-type"))

    def _data(self, data, start, end):
        if self._target is not None:
            self._target.write(data[start:end])
        elif self._name is not None:
            if len(self._buf) + end - start > MAX_FIELD_BYTES:
                raise Unprocessable(_("Form field too large"), code="invalid_dataset_field")
            self._buf += data[start:end]

    def _end(self):
        if self._target is None and self._name:
            self.fields[self._name] = bytes(self._buf).decode("utf-8", "replace")
        self._target = None


@router.post("/upload")
async def upload(request: Request, user: UiUser = Depends(require_user)):
    ctype, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary:
        raise HTTPException(400, _("Expected a multipart form upload"))
    expected = request.session.get("csrf")

    def on_file(fields: dict, filename: str, media_type: str | None):
        sent = request.headers.get("X-CSRF-Token") or fields.get("csrf_token")
        if not expected or not sent or not secrets.compare_digest(str(sent), expected):
            raise HTTPException(403, _("Invalid CSRF token"))
        if not filename:
            raise Unprocessable(_("Choose a file"), code="invalid_dataset_field")
        with web_session() as s:
            datasets.project_for_upload(s, fields.get("project"), ui_actor(s, user))
            return datasets.begin_upload(s, filename, media_type)

    form = _StreamForm(on_file)
    try:
        with web_session() as s:
            limit = datasets.max_upload_bytes(s)
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > limit + UPLOAD_SLACK:
            raise Unprocessable(_("The file is larger than %(size)d MB", size=limit // (1024 * 1024)),
                                code="dataset_too_large")
        parser = MultipartParser(boundary, form.callbacks())
        async for chunk in request.stream():
            # Encryption and disk writes run in the thread pool, not on the event loop.
            await run_in_threadpool(parser.write, chunk)
        parser.finalize()
        pending = form.file
        if pending is None:
            raise Unprocessable(_("Choose a file"), code="invalid_dataset_field")
        await run_in_threadpool(pending.finish)
        upload_form = datasets.parse_form(form.fields)
        try:
            with web_session() as s:
                me = ui_actor(s, user)
                project = datasets.project_for_upload(s, form.fields.get("project"), me)
                stored = datasets.store(s, me, project, pending, upload_form)
                key, dup = stored.dataset.dataset_key, stored.duplicate_of
        except BaseException:
            datasets.discard(pending.stored_path)  # Moved into the store, but the row was not committed
            raise
    except AppError as e:
        if form.file is not None:
            form.file.abort()
        return _upload_page(request, user, e.status, values=form.fields, error=e.message)
    except BaseException:
        if form.file is not None:
            form.file.abort()
        raise
    target = f"/data/{key}?uploaded=1" + (f"&duplicate={quote(dup)}" if dup else "")
    return RedirectResponse(target, 303)


@router.get("/results/{result_id}")
def result_view(request: Request, result_id: int, user: UiUser = Depends(require_user), msg: str = "",
                err: str = ""):
    """Release view of the output gate (research/konzept-datenzonen.md §8): checks, full output, logs, decision."""
    with web_session() as s:
        me = ui_actor(s, user)
        result, task, project = output_gate.get_visible(s, result_id, me)
        ctx = output_gate.view(s, result, task, project, me)
        entries = data_audit.for_object(s, "result", result.id)
        return render(request, "data_result.html", **ctx, message=msg, error=err, entries=entries,
                      details={e.id: data_audit.detail(e) for e in entries},
                      actor_names=datasets.audit_actor_names(s, entries))


@router.post("/results/{result_id}/decide", dependencies=POST)
def result_decide(request: Request, result_id: int, user: UiUser = Depends(require_user),
                  decision: str = Form(""), note: str = Form(""), noise_reviewed: str = Form(""),
                  policy_token: str = Form("")):
    try:
        with web_session() as s:
            me = ui_actor(s, user)
            output_gate.get_visible(s, result_id, me)
            status = output_gate.decide(s, result_id, me, decision, note,
                                        noise_reviewed == "1", policy_token).status
    except AppError as e:
        if e.status == 404:
            raise
        return RedirectResponse(f"/data/results/{result_id}?err={quote(e.message)}", 303)
    done = {"released": _("Result #%(id)s released.", id=result_id),
            "rejected": _("Result #%(id)s rejected.", id=result_id),
            "kept_sealed": _("Result #%(id)s stays in zone R.", id=result_id)}[status]
    return RedirectResponse(f"/data/results/{result_id}?msg={quote(done)}", 303)


@router.get("/{key}")
def detail(request: Request, key: str, user: UiUser = Depends(require_user), uploaded: str = "",
           duplicate: str = "", err: str = ""):
    with web_session() as s:
        me = ui_actor(s, user)
        ds, project = datasets.get_visible(s, key, me)
        entries = data_audit.for_object(s, "dataset", ds.id)
        profile = dataset_profiles.latest(s, ds)
        if profile and profile.status != "released" and not datasets.can_upload(s, project, me):
            profile = None
        if profile:
            entries = dataset_profiles.audit_entries(s, ds)
        names = datasets.uploaded_names(s, [ds])
        replaced = s.get(type(ds), ds.supersedes_id) if ds.supersedes_id else None
        message = None
        if uploaded:
            message = _("%(key)s is stored encrypted. A profile is created once the runner is set up.", key=ds.dataset_key)
            if duplicate:
                message += " " + _("Same content as %(other)s.", other=duplicate)
        return render(request, "data_detail.html", ds=ds, project=project, entries=entries,
                      privacy=gate_privacy.view(ds), error=err,
                      can_configure_policy=datasets.can_delete(project, me) and ds.status != "deleted",
                      profile=profile, profile_schema=json.loads(profile.schema_json) if profile else None,
                      code_task=s.get(Task, profile.code_task_id) if profile and profile.code_task_id else None,
                      profile_status={"generated": _("Awaiting profile release"), "released": _("Released"),
                                      "rejected": _("Rejected")},
                      profile_self_release_blocked=(ds.data_class == "sealed" and me.id == ds.uploaded_by
                                                    and output_gate.four_eyes(s, project)),
                      details={e.id: data_audit.detail(e) for e in entries},
                      actor_names=datasets.audit_actor_names(s, entries), names=names, replaced=replaced,
                      can_delete=datasets.can_delete(project, me) and ds.status != "deleted",
                      can_download=datasets.can_download(me) and ds.status != "deleted",
                      retention_left=datasets.retention_left(ds), message=message,
                      runs=output_gate.for_dataset(s, ds), **_labels())


@router.post("/{key}/output-policy", dependencies=POST)
def output_policy(request: Request, key: str, user: UiUser = Depends(require_user),
                  query_limit: str = Form(""), epsilon_limit: str = Form(""), epsilon: str = Form(""),
                  sensitivity: str = Form("")):
    try:
        with web_session() as s:
            me = ui_actor(s, user)
            ds, _project = datasets.get_visible(s, key, me)
            gate_privacy.configure(s, ds, me, {"query_limit": query_limit, "epsilon_limit": epsilon_limit,
                                              "epsilon": epsilon, "sensitivity": sensitivity})
    except AppError as e:
        if e.status == 404:
            raise
        return RedirectResponse(f"/data/{quote(key)}?err={quote(e.message)}", 303)
    return RedirectResponse(f"/data/{quote(key)}", 303)


@router.post("/{key}/profile", dependencies=POST)
async def decide_profile(request: Request, key: str, user: UiUser = Depends(require_user)):
    form = await request.form()
    aliases = {k[6:]: str(v).strip() for k, v in form.items() if k.startswith("alias_") and str(v).strip()}
    profile_id = str(form.get("profile_id", ""))
    if not profile_id.isdigit():
        raise Unprocessable(_("This profile is no longer awaiting a decision."), code="profile_decided")
    with web_session() as s:
        me = ui_actor(s, user)
        ds, project = datasets.get_visible(s, key, me)
        dataset_profiles.decide(s, ds, me, str(form.get("decision", "")), aliases, int(profile_id))
    return RedirectResponse(f"/data/{key}", 303)


@router.post("/{key}/delete", dependencies=POST)
def delete(request: Request, key: str, user: UiUser = Depends(require_user), confirm: str = Form("")):
    with web_session() as s:
        me = ui_actor(s, user)
        ds, _project = datasets.get_visible(s, key, me)
        if confirm.strip().upper() != ds.dataset_key:
            raise Unprocessable(_("Type the dataset key %(key)s to confirm the deletion", key=ds.dataset_key),
                                code="confirm_required")
        datasets.delete(s, ds, me, via="web", reason="manual")
    return RedirectResponse(f"/data/{ds.dataset_key}", 303)


@router.post("/{key}/download", dependencies=POST)
def download(request: Request, key: str, user: UiUser = Depends(require_user), reason: str = Form("")):
    with web_session() as s:
        me = ui_actor(s, user)
        ds, _project = datasets.get_visible(s, key, me)
        path, dek = datasets.open_download(s, ds, me, reason)  # Audited before the first byte leaves
        name = ds.original_filename or f"{ds.dataset_key}.bin"
        media = ds.media_type or "application/octet-stream"
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}", "Cache-Control": "no-store"}
    return StreamingResponse(datasets.plaintext_chunks(path, dek), media_type=media, headers=headers)
