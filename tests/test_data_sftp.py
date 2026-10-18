"""Native SFTP inbox (research/konzept-datenzonen.md §4): account and key handling, the import service and the
`tbr-admin data sftp-user` CLI. Unit tests need no DB; import flows and the CLI run against the test DB.
Synthetic data only."""
import hashlib
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from app import data_crypto
from app.config import get_settings
from app.db import db
from app.models import DataSftpAccount, DataSftpKey, Project
from app.services import data_audit, data_import, data_sftp, datasets, settings_store
from tests.test_data_store import CSV, PW, _audit, _ds, denv, en  # noqa: F401 (fixtures + helpers)
from tests.test_workflow import env, gitea, mkproject  # noqa: F401 (fixtures)

ED_KEY = "ssh-ed25519 kdLQ2Arux9FeA8znWRPwhgz+l91RhQ+kcWq56Nt8mes9rKnOAQFqQw== export@workstation"
# A second, distinct valid ed25519 key body (only the body must differ; the blob is not validated further).
ED_KEY2 = "ssh-ed25519 Mz7PmOEAowZerLKVKIxAAd0DvDRUegodC5P+0b7V2eybpvIAdRcArA== export@laptop"
RSA_KEY = ("ssh-rsa YtwF/1C1Cv/0rI1zP3mF6xh8BHWw+y6u3e9OkaBlUpq5jdRT8kWqsjkbdDzFcSbIfUS9St1hxxqnD8cxqntO"
           "WvUPVxFJNUlLgnJaZ2zQLDJI7wjewBv5guiGrfXIHaLLkTjZgFF33mB+AFcsbfSe1GFO/B/fwGqTazYMjcSH5w/UoR29"
           "ORhDklHRGJyjwzaN8Qu1YRrS65vwUgUr4fl4mJacSyYs8oCXGfpEt8ndR9ZGoOw0+1/Mqx8OyXt7cPffLVAstPrIU5o="
           " export@nas")


# --- Key parsing (no DB) ---------------------------------------------------------------------------------------

def test_parse_key_accepts_openssh_lines(en):  # noqa: F811 – English messages (app/i18n)
    key = data_sftp.parse_key(ED_KEY)
    assert key.key_type == "ssh-ed25519"
    assert key.comment == "export@workstation"
    assert key.fingerprint.startswith("SHA256:") and "=" not in key.fingerprint
    assert data_sftp.parse_key(RSA_KEY).key_type == "ssh-rsa"
    # A leading comment line and blank lines are skipped; options are refused.
    assert data_sftp.parse_key(f"# my key\n\n{ED_KEY}").key_type == "ssh-ed25519"
    for bad in ("", "not a key", "ssh-ed25519 !!!notbase64", "command=\"x\" ssh-ed25519 AAAA test"):
        with pytest.raises(Exception, match="public key|Base64|OpenSSH"):
            data_sftp.parse_key(bad)


def test_read_key_accepts_a_pub_file(tmp_path):
    path = tmp_path / "id_ed25519.pub"
    path.write_text(ED_KEY + "\n")
    assert data_sftp.read_key(str(path)).comment == "export@workstation"
    assert data_sftp.read_key(ED_KEY).fingerprint == data_sftp.parse_key(ED_KEY).fingerprint


def test_authorized_keys_forces_sftp_and_uses_fingerprint():
    account = SimpleNamespace(username="demo-export")
    key = DataSftpKey(key_type="ssh-ed25519", key_blob="AAAA", fingerprint="SHA256:abc")
    body = data_sftp.authorized_keys(account, [key])
    assert body.startswith('restrict,command="/usr/lib/openssh/sftp-server" ssh-ed25519 AAAA SHA256:abc')
    assert body.endswith("\n")


# --- Finished-file detection (no DB) ---------------------------------------------------------------------------

def _account(tmp_path, name="demo-export") -> DataSftpAccount:
    return DataSftpAccount(id=1, username=name, project_id=1, owner_id=1)


def test_pending_files_only_lists_finished_files(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "tbr_sftp_dir", tmp_path)
    import time

    account = _account(tmp_path)
    folder = data_sftp.upload_path(account.username)
    folder.mkdir(parents=True)
    (folder / "old.csv").write_bytes(b"a;b\n")
    old = time.time() - data_import.SETTLE_SECONDS - 5
    import os
    os.utime(folder / "old.csv", (old, old))
    (folder / "fresh.csv").write_bytes(b"a;b\n")  # Changed just now: not finished yet
    (folder / ".hidden").write_bytes(b"x")
    (folder / "part.csv.part").write_bytes(b"x")
    (folder / "marker.csv.done").write_bytes(b"a;b\n")
    names = sorted(p.name for p in data_import.pending_files(account))
    assert names == ["marker.csv.done", "old.csv"]
    # A `.done` marker is stripped from the dataset name; the plain name is kept otherwise.
    assert data_import._plain_name("report.csv.done") == "report.csv"
    assert data_import._plain_name("report.csv") == "report.csv"


# --- Flows with DB ---------------------------------------------------------------------------------------------

@pytest.fixture
def sfenv(denv, tmp_path, monkeypatch):  # noqa: F811
    """DEMO with the data store on (denv) plus an SFTP root and a provisioned account row."""
    root = tmp_path / "sftp"
    monkeypatch.setattr(get_settings(), "tbr_sftp_dir", root)
    with db.session() as s:
        project = s.scalar(select(Project).where(Project.project_key == "DEMO"))
        account = DataSftpAccount(username="demo-export", project_id=project.id, owner_id=project.owner_id,
                                  name="Accounting export")
        s.add(account)
        s.flush()
        s.add(DataSftpKey(account_id=account.id, key_type="ssh-ed25519",
                          key_blob=ED_KEY.split()[1], fingerprint=data_sftp.parse_key(ED_KEY).fingerprint,
                          comment="export@workstation"))
        s.commit()
    for account in (Path(root) / "demo-export",):
        (account / "upload").mkdir(parents=True, exist_ok=True)
    denv["sftp"] = root
    return denv


def _drop(env, name, content=CSV, *, age=None):
    import os

    path = env["sftp"] / "demo-export" / "upload" / name
    path.write_bytes(content)
    if age is not None:
        os.utime(path, (age, age))
    return path


def _old() -> float:
    import time

    return time.time() - data_import.SETTLE_SECONDS - 5


@pytest.mark.db
def test_import_encrypts_and_removes_plaintext(sfenv, en):  # noqa: F811
    drop = _drop(sfenv, "bookings.csv", age=_old())
    with db.session() as s:
        result = data_import.scan(s)
    assert result["imported"] == 1 and result["users"] == 1 and not drop.exists()
    key = result["files"][0]["dataset"]
    ds = _ds(key)
    assert (ds.status, ds.upload_channel, ds.size_bytes) == ("profiling", "sftp", len(CSV))
    assert ds.sha256 == hashlib.sha256(CSV).hexdigest() and ds.original_filename == "bookings.csv"
    assert ds.retention_until == date.today() + timedelta(days=90)
    blob = (sfenv["data"] / ds.storage_path).read_bytes()
    assert b"booking_date" not in blob
    dek = data_crypto.unwrap_dek(datasets.kek(), ds.dek_wrapped, ds.dataset_key)
    assert data_crypto.decrypt_file(sfenv["data"] / ds.storage_path, dek) == CSV
    [entry] = _audit("dataset_uploaded")
    assert entry.via == "sftp" and data_audit.detail(entry)["dataset"] == key
    with db.session() as s:
        account = s.scalar(select(DataSftpAccount).where(DataSftpAccount.username == "demo-export"))
        assert account.last_import_at is not None
    # A second run without changes imports nothing.
    with db.session() as s:
        assert data_import.scan(s) == {"imported": 0, "failed": 0, "skipped": 0, "users": 0}


@pytest.mark.db
def test_import_skips_fresh_and_reports_duplicates(sfenv):
    _drop(sfenv, "fresh.csv")  # Just written: not settled
    with db.session() as s:
        assert data_import.scan(s).get("imported", 0) == 0
    _drop(sfenv, "first.csv", age=_old())
    _drop(sfenv, "second.csv", age=_old())
    with db.session() as s:
        result = data_import.scan(s)
    assert result["imported"] == 2
    key = result["files"][0]["dataset"]
    assert result["files"][1]["duplicate"] == key  # Same content
    assert (sfenv["sftp"] / "demo-export" / "upload" / "fresh.csv").exists()


@pytest.mark.db
def test_import_respects_the_data_store_switch(sfenv, en):  # noqa: F811
    _drop(sfenv, "bookings.csv", age=_old())
    with db.session() as s:
        settings_store.update(s, {"data_enabled": "false"}, None)
        s.commit()
    with db.session() as s:
        assert data_import.scan(s) == {"skipped": "disabled"}
    assert (sfenv["sftp"] / "demo-export" / "upload" / "bookings.csv").exists()
    assert _audit("dataset_uploaded") == []


@pytest.mark.db
def test_import_reports_an_empty_file_and_keeps_it(sfenv, en):  # noqa: F811
    _drop(sfenv, "empty.csv", b"", age=_old())
    with db.session() as s:
        result = data_import.scan(s)
    assert result["failed"] == 1 and result["imported"] == 0
    assert (sfenv["sftp"] / "demo-export" / "upload" / "empty.csv").exists()


# --- CLI -------------------------------------------------------------------------------------------------------

@pytest.mark.db
def test_cli_add_list_and_remove_keys(sfenv, en):  # noqa: F811 – English messages in the assertions
    from cli.tm_admin import app as admin_cli

    runner = CliRunner()
    out = runner.invoke(admin_cli, ["data", "sftp-user", "list"])
    assert out.exit_code == 0 and "demo-export" in out.output, out.output
    out = runner.invoke(admin_cli, ["data", "sftp-user", "key", "add", "demo-export", ED_KEY2, "--no-provision"])
    assert out.exit_code == 0 and "added" in out.output, out.output
    out = runner.invoke(admin_cli, ["data", "sftp-user", "key", "list", "demo-export"])
    assert out.exit_code == 0 and out.output.count("SHA256:") == 2, out.output
    fingerprint = data_sftp.parse_key(ED_KEY2).fingerprint
    out = runner.invoke(admin_cli, ["data", "sftp-user", "key", "remove", "demo-export", fingerprint])
    assert out.exit_code == 0, out.output
    with db.session() as s:
        keys = data_sftp.keys_of(s, data_sftp.get(s, "demo-export"))
    assert len(keys) == 1
    # Removing the last key is refused (the user would be unreachable).
    out = runner.invoke(admin_cli, ["data", "sftp-user", "key", "remove", "demo-export", keys[0].fingerprint])
    assert out.exit_code == 1 and "without a key" in out.output, out.output


@pytest.mark.db
def test_cli_add_and_remove_a_user(sfenv, en):  # noqa: F811
    from cli.tm_admin import app as admin_cli

    runner = CliRunner()
    out = runner.invoke(admin_cli, ["data", "sftp-user", "add", "report-bot", "--project", "DEMO",
                                    "--key", ED_KEY, "--no-provision"])
    assert out.exit_code == 0 and "created" in out.output, out.output
    with db.session() as s:
        project = s.scalar(select(Project).where(Project.project_key == "DEMO"))
        added = s.scalar(select(DataSftpAccount).where(DataSftpAccount.username == "report-bot"))
        assert added.project_id == project.id and added.owner_id == project.owner_id
    # A duplicate name is refused.
    out = runner.invoke(admin_cli, ["data", "sftp-user", "add", "report-bot", "--project", "DEMO",
                                    "--no-provision"])
    assert out.exit_code == 1 and "already exists" in out.output, out.output
    out = runner.invoke(admin_cli, ["data", "sftp-user", "remove", "report-bot"])
    assert out.exit_code == 0 and "removed" in out.output, out.output
    with db.session() as s:
        assert data_sftp.find(s, "report-bot") is None


@pytest.mark.db
def test_cli_import_reports_imported_files(sfenv, en):  # noqa: F811
    from cli.tm_admin import app as admin_cli

    _drop(sfenv, "bookings.csv", age=_old())
    out = CliRunner().invoke(admin_cli, ["data", "sftp-user", "import"])
    assert out.exit_code == 0 and "1 imported" in out.output, out.output
    assert "DS-" in out.output


@pytest.mark.db
def test_cli_rejects_an_invalid_user_name(sfenv, en):  # noqa: F811
    from cli.tm_admin import app as admin_cli

    out = CliRunner().invoke(admin_cli, ["data", "sftp-user", "add", "Bad Name", "--project", "DEMO",
                                         "--no-provision"])
    assert out.exit_code == 1 and "Invalid user name" in out.output, out.output


@pytest.mark.db
def test_audit_records_account_and_key_actions(sfenv):
    from cli.tm_admin import app as admin_cli

    runner = CliRunner()
    runner.invoke(admin_cli, ["data", "sftp-user", "add", "report-bot", "--project", "DEMO", "--key", ED_KEY,
                              "--no-provision"])
    actions = {r.action for r in _audit()}
    assert {"sftp_account_created", "sftp_key_added"} <= actions


@pytest.mark.db
def test_account_rows_are_unique_and_cascade(sfenv):
    with db.session() as s:
        account = s.scalar(select(DataSftpAccount).where(DataSftpAccount.username == "demo-export"))
        s.add(DataSftpKey(account_id=account.id, key_type="ssh-ed25519", key_blob="AAAA",
                          fingerprint="SHA256:dupe"))
        s.commit()
        with pytest.raises(Exception):
            s.add(DataSftpAccount(username="demo-export", project_id=account.project_id, owner_id=account.owner_id))
            s.commit()
        s.rollback()
    # Deleting an account removes its keys (ON DELETE CASCADE).
    with db.session() as s:
        account = s.scalar(select(DataSftpAccount).where(DataSftpAccount.username == "demo-export"))
        account_id = account.id
        s.delete(account)
        s.commit()
        assert s.scalars(select(DataSftpKey).where(DataSftpKey.account_id == account_id)).all() == []
