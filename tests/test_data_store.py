"""Data store (docs/data-store.md): envelope encryption, upload/list/rights, deletion and retention, data audit hash
chain. Unit tests need no DB; flows run against the test DB (UI texts in German). Synthetic data only."""
import hashlib
import io
import os
import stat
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from typer.testing import CliRunner

from app import data_crypto
from app.auth import hash_password
from app.config import get_settings
from app.db import db
from app.i18n import use_language
from app.models import Actor, DataAudit, Dataset, Project, ProjectMember
from app.services import data_audit, datasets, settings_store
from app.services.data_audit import GENESIS, row_hash, verify_rows
from tests.conftest import bearer, make_token
from tests.test_owner import human
from tests.test_sprint_editor import login_as
from tests.test_workflow import env, gitea, mkproject, mktask  # noqa: F401 (Fixtures)

PW = "a-long-test-password"
CSV = b"booking_date;account;amount\n2026-01-02;4400;12.50\n2026-01-03;4400;99.00\n"


# --- Encryption (no DB) --------------------------------------------------------------------------------------

@pytest.fixture
def en():
    with use_language("en"):
        yield

def _encrypt(data: bytes, dek: bytes, chunk: int = 64) -> bytes:
    out = io.BytesIO()
    w = data_crypto.EncryptingWriter(out, dek, chunk_size=chunk)
    for i in range(0, len(data), 7):  # Odd write sizes cross chunk borders.
        w.write(data[i:i + 7])
    w.close()
    assert w.size == len(data) and w.sha256 == hashlib.sha256(data).hexdigest()
    return out.getvalue()


def _decrypt(blob: bytes, dek: bytes) -> bytes:
    return b"".join(data_crypto.decrypt_chunks(io.BytesIO(blob), dek))


@pytest.mark.parametrize("size", [0, 1, 63, 64, 65, 64 * 3, 64 * 3 + 5])
def test_round_trip_any_size(size):
    data = os.urandom(size)
    dek = data_crypto.new_dek()
    blob = _encrypt(data, dek)
    assert _decrypt(blob, dek) == data
    if size >= 16:
        assert data not in blob  # Only ciphertext is written


def test_default_chunk_size_round_trip():
    data = os.urandom(data_crypto.CHUNK_SIZE + 10)
    dek = data_crypto.new_dek()
    assert _decrypt(_encrypt(data, dek, data_crypto.CHUNK_SIZE), dek) == data


def test_wrong_dek_fails(en):
    blob = _encrypt(CSV, data_crypto.new_dek())
    with pytest.raises(data_crypto.DecryptError, match="wrong key"):
        _decrypt(blob, data_crypto.new_dek())


def test_tampered_file_fails(en):
    dek = data_crypto.new_dek()
    blob = bytearray(_encrypt(os.urandom(200), dek))
    header, record = 27, 5 + 12 + 64 + 16
    flipped = bytearray(blob)
    flipped[header + record - 3] ^= 1
    swapped = blob[:header] + blob[header + record:header + 2 * record] + blob[header:header + record] + \
        blob[header + 2 * record:]
    for broken in (bytes(flipped), bytes(swapped), bytes(blob[:-1]), bytes(blob[:header + record]),
                   bytes(blob) + b"x", b"not a dataset"):
        with pytest.raises(data_crypto.DecryptError):
            _decrypt(broken, dek)
    final_cut = blob[:header] + blob[header:header + 3 * record]  # Drop the final record exactly
    with pytest.raises(data_crypto.DecryptError, match="truncated"):
        _decrypt(bytes(final_cut), dek)


def test_kek_file_and_wrapping(tmp_path, en):
    path = tmp_path / "etc" / "data.key"
    with pytest.raises(data_crypto.DataKeyError, match="init-data-key"):
        data_crypto.load_kek(path)
    assert data_crypto.create_kek(path) is True
    assert data_crypto.create_kek(path) is False  # Never overwritten
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    kek = data_crypto.load_kek(path)
    assert len(kek) == 32
    dek = data_crypto.new_dek()
    wrapped = data_crypto.wrap_dek(kek, dek, "DS-0001")
    assert len(wrapped) == data_crypto.WRAPPED_BYTES <= 128
    assert data_crypto.unwrap_dek(kek, wrapped, "DS-0001") == dek
    with pytest.raises(data_crypto.DecryptError, match="different data key"):
        data_crypto.unwrap_dek(os.urandom(32), wrapped, "DS-0001")
    with pytest.raises(data_crypto.DecryptError, match="does not belong"):
        data_crypto.unwrap_dek(kek, wrapped, "DS-0002")  # Copied to another row
    with pytest.raises(data_crypto.DecryptError, match="deleted"):
        data_crypto.unwrap_dek(kek, None, "DS-0001")
    os.chmod(path, 0o644)
    with pytest.raises(data_crypto.DataKeyError, match="other users"):
        data_crypto.load_kek(path)
    raw = tmp_path / "raw.key"
    raw.write_bytes(os.urandom(32))
    os.chmod(raw, 0o600)
    assert len(data_crypto.load_kek(raw)) == 32
    raw.write_bytes(b"too short\n")
    with pytest.raises(data_crypto.DataKeyError, match="32 bytes"):
        data_crypto.load_kek(raw)


# --- Audit chain (no DB) -------------------------------------------------------------------------------------

def _chain(n: int) -> list[SimpleNamespace]:
    rows, prev = [], GENESIS
    for i in range(1, n + 1):
        row = SimpleNamespace(id=i, at=datetime(2026, 10, 16, 8, 0, i, 1234), actor_id=1, action="dataset_uploaded",
                              object_type="dataset", object_id=i, task_id=None, detail_json=f'{{"n": {i}}}',
                              via="web", prev_hash=prev)
        row.hash = prev = row_hash(prev, row)
        rows.append(row)
    return rows


def test_audit_chain_detects_tampering():
    rows = _chain(5)
    head = (5, rows[-1].hash)
    assert verify_rows(rows, head).ok and verify_rows(rows, head).count == 5
    assert verify_rows([], (0, GENESIS)).ok
    changed = _chain(5)
    changed[2].detail_json = '{"n": 99}'
    v = verify_rows(changed, head)
    assert (v.ok, v.bad_id, v.reason, v.count) == (False, 3, "hash", 2)
    v = verify_rows(rows[:2] + rows[3:], head)  # Row 3 deleted
    assert (v.ok, v.bad_id, v.reason) == (False, 4, "prev_hash")
    v = verify_rows(rows[:4], head)  # Last row cut off: only the head notices
    assert (v.ok, v.reason) == (False, "head")
    rehashed = _chain(5)
    rehashed[1].action = "dataset_deleted"
    rehashed[1].hash = row_hash(rehashed[1].prev_hash, rehashed[1])  # Recomputed hash breaks the next link
    v = verify_rows(rehashed, head)
    assert (v.ok, v.bad_id, v.reason) == (False, 3, "prev_hash")


# --- Flows with DB -------------------------------------------------------------------------------------------

@pytest.fixture
def denv(env, tmp_path, monkeypatch):  # noqa: F811
    """DEMO (local_only, owner alex); bob member, carol not; data store on with a fresh key in tmp_path."""
    for name in ("alex", "bob", "carol"):
        human(env, name, password=PW)
    mkproject(env, key="DEMO", privacy="local_only", owner="alex")
    with db.session() as s:
        project = s.scalar(select(Project).where(Project.project_key == "DEMO"))
        bob = s.scalar(select(Actor).where(Actor.name == "bob"))
        s.add(ProjectMember(project_id=project.id, actor_id=bob.id))
        s.scalar(select(Actor).where(Actor.name == "admin")).password_hash = hash_password(PW)
        settings_store.update(s, {"data_enabled": "true"}, None)
        s.commit()
    monkeypatch.setattr(get_settings(), "tbr_data_dir", tmp_path / "data")
    monkeypatch.setattr(get_settings(), "tbr_data_key_file", tmp_path / "key" / "data.key")
    data_crypto.create_kek(tmp_path / "key" / "data.key")
    env["data"] = tmp_path / "data"
    return env


def _upload(c, content=CSV, filename="bookings.csv", csrf=True, **fields):
    data = {"csrf_token": c.csrf if csrf else "", "project": "DEMO", "name": "Bookings Q1", "data_class": "sealed",
            "retention_days": "", "min_group_size": "", **fields}
    return c.post("/data/upload", data=data, files={"file": (filename, content, "text/csv")}, follow_redirects=False)


def _stored(c, **kw) -> str:
    """Upload and return the new dataset key."""
    r = _upload(c, **kw)
    assert r.status_code == 303, r.text
    return r.headers["location"].split("/data/")[1].split("?")[0]


def _ds(key) -> Dataset | None:
    with db.session() as s:
        return s.scalar(select(Dataset).where(Dataset.dataset_key == key))


def _count() -> int:
    with db.session() as s:
        return len(s.scalars(select(Dataset.id)).all())


def _set_actor(name, **values):
    with db.session() as s:
        actor = s.scalar(select(Actor).where(Actor.name == name))
        for k, v in values.items():
            setattr(actor, k, v)
        s.commit()


def _audit(action=None):
    with db.session() as s:
        return [r for r in s.scalars(select(DataAudit).order_by(DataAudit.id)) if action in (None, r.action)]


def _files(e, folder):
    path = e["data"] / folder
    return sorted(path.iterdir()) if path.exists() else []


@pytest.mark.db
def test_upload_stores_only_ciphertext_and_round_trips(denv):
    alex = login_as("alex", PW)
    assert 'href="/data"' in alex.get("/backlog").text  # Owner sees the menu item "Data"
    r = _upload(alex)
    assert r.status_code == 303 and "?uploaded=1" in r.headers["location"], r.text
    key = r.headers["location"].split("/data/")[1].split("?")[0]
    ds = _ds(key)
    assert (ds.status, ds.data_class, ds.size_bytes, ds.upload_channel) == ("profiling", "sealed", len(CSV), "web")
    assert ds.sha256 == hashlib.sha256(CSV).hexdigest() and ds.original_filename == "bookings.csv"
    assert ds.retention_until == date.today() + timedelta(days=90) and ds.storage_path == f"store/{key}.enc"
    assert key == f"DS-{ds.id:04d}"
    blob = (denv["data"] / ds.storage_path).read_bytes()
    assert b"booking_date" not in blob and b"4400" not in blob
    assert _files(denv, "incoming") == []
    dek = data_crypto.unwrap_dek(datasets.kek(), ds.dek_wrapped, ds.dataset_key)
    assert data_crypto.decrypt_file(denv["data"] / ds.storage_path, dek) == CSV
    [entry] = _audit("dataset_uploaded")
    assert entry.object_id == ds.id and "Bookings" not in (entry.detail_json or "")  # No names/values in the audit
    page = alex.get("/data").text
    assert key in page and "Bookings Q1" in page and "Profil" in page
    detail = alex.get(r.headers["location"]).text
    assert "verschlüsselt gespeichert" in detail and "dataset_uploaded" in detail
    r = _upload(alex, name="Again")  # The same content again: stored, with a hint
    assert f"duplicate={key}" in r.headers["location"]
    assert f"Gleicher Inhalt wie {key}" in alex.get(r.headers["location"]).text


@pytest.mark.db
def test_rights_follow_the_concept(denv):
    key = _stored(login_as("alex", PW))
    bob, carol = login_as("bob", PW), login_as("carol", PW)
    # Member without the data right: sees the list and metadata, no menu item, no upload, no delete.
    assert 'href="/data"' not in bob.get("/backlog").text
    assert key in bob.get("/data").text and bob.get(f"/data/{key}").status_code == 200
    r = _upload(bob)
    assert r.status_code == 403 and "Datenrecht" in r.text and _count() == 1
    assert _files(denv, "incoming") == []
    assert f'action="/data/{key}/delete"' not in bob.get(f"/data/{key}").text
    assert bob.post(f"/data/{key}/delete", data={"csrf_token": bob.csrf, "confirm": key}).status_code == 403
    # Not a member: nothing visible.
    assert key not in carol.get("/data").text and carol.get(f"/data/{key}").status_code == 404
    assert _upload(carol).status_code == 404 and _count() == 1
    # With the data right the member may upload and sees the menu item.
    _set_actor("bob", data_access=True)
    assert 'href="/data"' in bob.get("/backlog").text
    _stored(bob, content=b"a;b\n1;2\n")
    # Raw download: admins with the data right only – not the owner.
    alex = login_as("alex", PW)
    assert alex.post(f"/data/{key}/download", data={"csrf_token": alex.csrf, "reason": "x"}).status_code == 403
    assert len(_audit("dataset_downloaded")) == 0


@pytest.mark.db
def test_csrf_size_limit_and_bad_fields(denv):
    alex = login_as("alex", PW)
    r = _upload(alex, csrf=False)
    assert r.status_code == 403 and _count() == 0 and _files(denv, "incoming") == []
    with db.session() as s:
        settings_store.update(s, {"data_max_upload_mb": "1"}, None)
        s.commit()
    r = _upload(alex, content=b"x" * (1024 * 1024 + 1))
    assert r.status_code == 422 and "größer als 1 MB" in r.text
    assert _count() == 0 and _files(denv, "incoming") == []
    r = _upload(alex, retention_days="0")
    assert r.status_code == 422 and _count() == 0 and _files(denv, "incoming") == []
    assert 'value="Bookings Q1"' in r.text  # Entered values stay in the form
    r = _upload(alex, content=b"")
    assert r.status_code == 422 and "Datei ist leer" in r.text and _count() == 0
    assert _files(denv, "store") == [] and _files(denv, "incoming") == []
    with db.session() as s:
        settings_store.update(s, {"data_enabled": "false"}, None)
        s.commit()
    assert _upload(alex).status_code == 422 and 'href="/data"' not in alex.get("/backlog").text


@pytest.mark.db
def test_admin_emergency_download_and_wrong_key(denv, tmp_path, monkeypatch):
    key = _stored(login_as("alex", PW))
    admin = login_as("admin", PW)
    assert "Notfall-Download" not in admin.get(f"/data/{key}").text  # Admin without the data right
    _set_actor("admin", data_access=True)
    assert "Notfall-Download" in admin.get(f"/data/{key}").text
    assert admin.post(f"/data/{key}/download", data={"csrf_token": admin.csrf, "reason": ""}).status_code == 422
    r = admin.post(f"/data/{key}/download", data={"csrf_token": admin.csrf, "reason": "auditor request"})
    assert r.status_code == 200 and r.content == CSV and "no-store" in r.headers["cache-control"]
    [entry] = _audit("dataset_downloaded")
    assert data_audit.detail(entry)["reason"] == "auditor request"
    # A different KEK: the wrapped DEK names the mismatch instead of returning garbage.
    other = tmp_path / "other.key"
    data_crypto.create_kek(other)
    monkeypatch.setattr(get_settings(), "tbr_data_key_file", other)
    r = admin.post(f"/data/{key}/download", data={"csrf_token": admin.csrf, "reason": "again"})
    assert r.status_code == 422 and "anderen Datenschlüssel" in r.text
    assert len(_audit("dataset_downloaded")) == 1


@pytest.mark.db
def test_delete_shreds_key_and_file(denv):
    alex = login_as("alex", PW)
    old = _stored(alex)
    new = _stored(alex, content=b"v2;x\n", supersedes=old, name="Bookings Q1 v2")
    assert (_ds(old).status, _ds(new).supersedes_id) == ("archived", _ds(old).id)
    assert len(_audit("dataset_archived")) == 1
    r = alex.post(f"/data/{old}/delete", data={"csrf_token": alex.csrf, "confirm": "wrong"})
    assert r.status_code == 422 and _ds(old).status == "archived"
    r = alex.post(f"/data/{old}/delete", data={"csrf_token": alex.csrf, "confirm": old.lower()},
                  follow_redirects=True)
    ds = _ds(old)
    assert (ds.status, ds.dek_wrapped, ds.deleted_by is not None) == ("deleted", None, True)
    assert not (denv["data"] / "store" / f"{old}.enc").exists() and (denv["data"] / "store" / f"{new}.enc").exists()
    assert "Datensatzschlüssel wurde vernichtet" in r.text and "dataset_deleted" in r.text
    [entry] = _audit("dataset_deleted")
    assert data_audit.detail(entry) == {"dataset": old, "reason": "manual", "file_removed": True}
    assert f"/data/{old}" not in alex.get("/data").text and f"/data/{old}" in alex.get("/data?status=all").text
    # A deleted dataset cannot be replaced or downloaded any more.
    assert _upload(alex, content=b"v3\n", supersedes=old).status_code == 422


@pytest.mark.db
def test_retention_job_warns_then_deletes(denv):
    from cli.tm_admin import app as admin_cli

    alex = login_as("alex", PW)
    key, other = _stored(alex), _stored(alex, content=b"other\n", name="Later")
    today = date.today()
    with db.session() as s:
        s.scalar(select(Dataset).where(Dataset.dataset_key == key)).retention_until = today + timedelta(days=3)
        s.commit()
    with db.session() as s:
        assert datasets.retention_sweep(s, today) == {"warned": 1}
        s.commit()
    with db.session() as s:
        assert datasets.retention_sweep(s, today) == {}  # Reminder only once
        s.commit()
    assert _ds(key).retention_warned_at is not None and len(_audit("dataset_retention_warning")) == 1
    assert "wird in 3 Tagen gelöscht" in alex.get("/data").text
    with db.session() as s:
        assert datasets.retention_sweep(s, today + timedelta(days=4)) == {"deleted": 1}
        s.commit()
    ds = _ds(key)
    assert (ds.status, ds.dek_wrapped, ds.deleted_by) == ("deleted", None, None)
    assert not (denv["data"] / "store" / f"{key}.enc").exists() and _ds(other).status == "profiling"
    [entry] = _audit("dataset_deleted")
    assert (entry.actor_id, entry.via, data_audit.detail(entry)["reason"]) == (None, "scheduler", "retention")
    # The CLI runs the same job (e.g. from a timer); nothing is due now.
    out = CliRunner().invoke(admin_cli, ["data", "retention"])
    assert out.exit_code == 0 and "0 deleted" in out.output, out.output
    with db.session() as s:
        settings_store.update(s, {"data_enabled": "false"}, None)
        assert datasets.retention_sweep(s, today + timedelta(days=400)) == {}  # Store switched off
        s.commit()
    assert _ds(other).status == "profiling"


@pytest.mark.db
def test_audit_chain_in_db_and_tamper_detection(denv):
    from cli.tm_admin import app as admin_cli

    alex = login_as("alex", PW)
    key = _stored(alex)
    alex.post(f"/data/{key}/delete", data={"csrf_token": alex.csrf, "confirm": key})
    with db.session() as s:
        verdict = data_audit.verify(s)
        triggers = data_audit.triggers_present(s)
    assert verdict.ok and verdict.count == 3
    out = CliRunner().invoke(admin_cli, ["data", "audit", "verify"])
    assert out.exit_code == 0 and "chain ok: 3 entries" in out.output, out.output
    assert triggers == sorted(data_audit.TRIGGERS)  # Test DB without binary logging: both triggers exist
    for sql in ("UPDATE data_audit SET detail_json = '{}' WHERE id = 1", "DELETE FROM data_audit WHERE id = 1"):
        with pytest.raises(Exception, match="append-only"):
            with db.engine.begin() as conn:
                conn.execute(text(sql))
    # Tampering with the trigger dropped (someone with DB rights): the chain still tells.
    with db.engine.begin() as conn:
        conn.execute(text("DROP TRIGGER data_audit_no_update"))
    try:
        with db.engine.begin() as conn:
            conn.execute(text("UPDATE data_audit SET detail_json = '{}' WHERE id = 1"))
        out = CliRunner().invoke(admin_cli, ["data", "audit", "verify"])
        assert out.exit_code == 1 and "BROKEN at entry 1" in out.output, out.output
        assert "data_audit_no_update" in out.output  # The missing trigger is reported
    finally:
        with db.engine.begin() as conn:
            conn.execute(text("CREATE TRIGGER data_audit_no_update BEFORE UPDATE ON data_audit FOR EACH ROW "
                              "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'data_audit is append-only'"))
    # A forged row appended directly (bypassing the head) is detected as well.
    with db.engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE data_audit"))
        conn.execute(text("UPDATE data_audit_head SET last_id = 0, last_hash = REPEAT('0', 64)"))
    with db.session() as s:
        data_audit.append(s, "dataset_uploaded", "dataset", 1, via="web")
        s.commit()
    with db.session() as s:
        assert data_audit.verify(s).ok
    with db.engine.begin() as conn:
        conn.execute(text("INSERT INTO data_audit (id, at, action, object_type, object_id, via, prev_hash, hash) "
                          "SELECT 2, NOW(6), 'forged', 'dataset', 1, 'web', hash, REPEAT('a', 64) "
                          "FROM data_audit WHERE id = 1"))
    with db.session() as s:
        v = data_audit.verify(s)
    assert (v.ok, v.bad_id, v.reason) == (False, 2, "hash")


@pytest.mark.db
def test_data_right_toggle_and_sealed_tasks(denv):
    from tests.test_ui_cli import post

    ui = login_as("admin", PW)
    assert 'action="/admin/users/bob/data-access"' in ui.get("/admin/users/bob").text
    assert 'action="/admin/users/claude-code/data-access"' not in ui.get("/admin/users/claude-code").text
    post(ui, "/admin/users/bob/data-access", data_access="1")
    with db.session() as s:
        assert s.scalar(select(Actor.data_access).where(Actor.name == "bob")) is True
    [entry] = _audit("data_access_changed")
    assert (entry.object_type, data_audit.detail(entry)) == ("actor", {"actor": "bob", "data_access": True})
    post(ui, "/admin/users/bob/data-access")  # Unchecked
    assert len(_audit("data_access_changed")) == 2
    # Sealed tasks: humans need the data right (research/konzept-datenzonen.md §1).
    nr = mktask(denv, project="DEMO", data_class="sealed")["task_nr"]
    patch = denv["c"].patch(f"/api/v1/tasks/{nr}", headers=denv["admin"], json={"assigned_to": "alex"})
    assert patch.status_code == 422 and patch.json()["error"]["code"] == "privacy_violation"
    _set_actor("alex", data_access=True)
    assert denv["c"].patch(f"/api/v1/tasks/{nr}", headers=denv["admin"],
                           json={"assigned_to": "alex"}).status_code == 200


def _make_members(project_key, *names):
    with db.session() as s:
        project = s.scalar(select(Project).where(Project.project_key == project_key))
        for name in names:
            agent = s.scalar(select(Actor).where(Actor.name == name))
            if s.get(ProjectMember, (project.id, agent.id)) is None:
                s.add(ProjectMember(project_id=project.id, actor_id=agent.id))
        s.commit()


@pytest.mark.db
def test_local_agents_read_local_only_datasets(denv):
    key = _stored(login_as("alex", PW), data_class="local_only")
    _make_members("DEMO", "opencode", "hermes")
    opencode, hermes = bearer(make_token("opencode")), bearer(make_token("hermes"))
    c = denv["c"]
    r = c.get(f"/api/v1/data/datasets/{key}/content", headers=opencode)
    assert r.status_code == 200 and r.content == CSV
    assert "attachment" in r.headers["content-disposition"] and "no-store" in r.headers["cache-control"]
    assert c.get(f"/api/v1/data/datasets/{key}/content", headers=hermes).status_code == 200
    reads = _audit("dataset_read")
    assert len(reads) == 2 and {e.via for e in reads} == {"api"}
    assert data_audit.detail(reads[0])["data_class"] == "local_only"
    # A local agent that is not a member does not even see the dataset.
    assert c.get(f"/api/v1/data/datasets/{key}/content", headers=bearer(make_token("openclaw-agent"))).status_code == 404


@pytest.mark.db
def test_cloud_agents_and_sealed_datasets_stay_blocked(denv):
    local_key = _stored(login_as("alex", PW), data_class="local_only")
    sealed_key = _stored(login_as("alex", PW), data_class="sealed")
    _make_members("DEMO", "opencode", "claude-code")
    c = denv["c"]
    r = c.get(f"/api/v1/data/datasets/{local_key}/content", headers=bearer(make_token("claude-code")))
    assert r.status_code == 403 and r.json()["error"]["code"] == "data_read_forbidden"
    r = c.get(f"/api/v1/data/datasets/{sealed_key}/content", headers=bearer(make_token("opencode")))
    assert r.status_code == 403 and r.json()["error"]["code"] == "data_read_forbidden"
    assert len(_audit("dataset_read")) == 0  # Refused reads are never audited.


@pytest.mark.db
def test_runner_agent_gets_403_on_sealed_and_local_only(denv):
    r = denv["c"].post("/api/v1/admin/actors", headers=denv["admin"], json={
        "name": "runner-1", "display_name": "Runner", "actor_type": "agent", "agent_kind": "runner"})
    assert r.status_code == 201, r.text
    runner = bearer(r.json()["token"])
    _make_members("DEMO", "runner-1")
    local_key = _stored(login_as("alex", PW), data_class="local_only")
    sealed_key = _stored(login_as("alex", PW), data_class="sealed")
    c = denv["c"]
    for key in (sealed_key, local_key):
        res = c.get(f"/api/v1/data/datasets/{key}/content", headers=runner)
        assert res.status_code == 403 and res.json()["error"]["code"] == "data_read_forbidden"
    assert len(_audit("dataset_read")) == 0  # Runner reads are never audited.


@pytest.mark.db
def test_agent_read_needs_a_local_model_when_enforced(denv):
    key = _stored(login_as("alex", PW), data_class="local_only")
    _make_members("DEMO", "hermes")
    with db.session() as s:
        s.scalar(select(Actor).where(Actor.name == "hermes")).model_location = "cloud"
        s.commit()
    r = denv["c"].get(f"/api/v1/data/datasets/{key}/content", headers=bearer(make_token("hermes")))
    assert r.status_code == 403 and r.json()["error"]["code"] == "data_read_forbidden"


@pytest.mark.db
def test_loosened_class_goes_to_the_data_audit(denv):
    r = denv["c"].post("/api/v1/tasks", headers=denv["admin"], json={
        "project": "DEMO", "title": "Script", "description": "d", "acceptance_criteria": "a", "data_class": "cloud_ok"})
    assert r.status_code == 201, r.text
    [entry] = _audit("data_class_lowered")
    assert entry.object_type == "project" and data_audit.detail(entry)["task"] == r.json()["task_nr"]
    assert entry.task_id is not None
