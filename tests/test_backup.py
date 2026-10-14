"""backup.sh components: option file from db.json and real mariadb-dump against the test DB."""
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from app import dbconfig
from cli import tm_admin
from tests.conftest import TEST_DB_URL, requires_db

pytestmark = [requires_db, pytest.mark.db]


def test_print_db_cnf_works_with_mariadb_dump(ready_db, settings, tmp_path):
    u = make_url(TEST_DB_URL)
    dbconfig.save(settings, dbconfig.DbConfig(u.host, u.port, u.database, u.username, u.password))
    runner = CliRunner()
    out = runner.invoke(tm_admin.app, ["print-db-cnf"]).stdout
    cnf = Path(out.strip())
    try:
        assert stat.S_IMODE(cnf.stat().st_mode) == 0o600
        assert u.password not in out  # stdout contains only the path.
        name = runner.invoke(tm_admin.app, ["db-name"]).stdout.strip()
        assert name == u.database
        if not shutil.which("mariadb-dump"):
            pytest.skip("mariadb-dump nicht installiert")
        r = subprocess.run(["mariadb-dump", f"--defaults-extra-file={cnf}", "--single-transaction", "--routines",
                            "--triggers", name], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert "CREATE TABLE `tasks`" in r.stdout
    finally:
        cnf.unlink(missing_ok=True)
