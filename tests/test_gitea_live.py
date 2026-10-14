"""GiteaClient against a real Gitea instance (throwaway repo deleted at the end).

Runs only with TBR_TEST_GITEA_TOKEN (service user's token). TBR_TEST_GITEA_COLLABORATOR
is optional (an existing Gitea user for the collaborator test)."""
import base64
import os
import secrets

import httpx
import pytest

from app.services.gitea import GiteaClient, GiteaError
from app.services.projects import REPO_FILES, render_repo_file

# TM_TEST_*: names before TaskBrigade 0.8.
GITEA = (os.environ.get("TBR_TEST_GITEA_URL") or os.environ.get("TM_TEST_GITEA_URL")
         or "http://gitea.example.internal:3001")
TOKEN = os.environ.get("TBR_TEST_GITEA_TOKEN") or os.environ.get("TM_TEST_GITEA_TOKEN")
COLLAB = os.environ.get("TBR_TEST_GITEA_COLLABORATOR") or os.environ.get("TM_TEST_GITEA_COLLABORATOR")

pytestmark = [pytest.mark.gitea_live,
              pytest.mark.skipif(not TOKEN, reason="TBR_TEST_GITEA_TOKEN nicht gesetzt")]


@pytest.fixture(scope="module")
def client():
    return GiteaClient(GITEA, TOKEN)


@pytest.fixture(scope="module")
def repo(client):
    me = client.current_user().login
    name = f"tm-selftest-{secrets.token_hex(4)}"
    r = client.create_repo(me, name, "TaskBrigade-Selbsttest (wird automatisch gelöscht)")
    yield r
    httpx.delete(f"{GITEA}/api/v1/repos/{r.owner}/{r.name}", headers={"Authorization": f"token {TOKEN}"},
                 timeout=15).raise_for_status()


def _commit_on_branch(repo, branch: str, path: str, content: str) -> None:
    """Simulate an agent push: commit on a new branch through the Contents API."""
    r = httpx.post(f"{GITEA}/api/v1/repos/{repo.owner}/{repo.name}/contents",
                   headers={"Authorization": f"token {TOKEN}"}, timeout=15,
                   json={"branch": "main", "new_branch": branch, "message": f"Änderung auf {branch}",
                         "files": [{"operation": "create", "path": path,
                                    "content": base64.b64encode(content.encode()).decode()}]})
    assert r.status_code == 201, r.text


class P:  # Minimal project object for repo templates.
    name, description, brain_path, default_branch = "Selbsttest", "Test", "brain.md", "main"


def test_live_full_repo_lifecycle(client, repo):
    me = client.current_user()
    assert client.version().startswith("1.")
    assert repo.default_branch == "main" and repo.owner == me.login

    # Project setup: files and branch protection.
    assert client.get_repo(repo.owner, repo.name).name == repo.name
    assert client.get_repo(repo.owner, "gibt-es-sicher-nicht-" + secrets.token_hex(3)) is None
    assert not client.file_exists(repo.owner, repo.name, "brain.md", "main")
    client.create_files(repo.owner, repo.name, {f: render_repo_file(f, project=P) for f in REPO_FILES}
                        | {"tasks/.gitkeep": ""}, "TaskBrigade: Initialdateien", "main")
    assert all(client.file_exists(repo.owner, repo.name, f, "main") for f in REPO_FILES)
    assert client.raw_file(repo.owner, repo.name, "brain.md", "main").startswith("# Selbsttest – Brain")
    client.protect_branch(repo.owner, repo.name, "main", me.login)
    client.protect_branch(repo.owner, repo.name, "main", me.login)  # idempotent
    assert any(r.name == repo.name for r in client.search_repos(repo.name))

    # Agent pushes task branch → submit → PR.
    branch = "task/SELF-0001-selbsttest"
    assert not client.branch_exists(repo.owner, repo.name, branch)
    _commit_on_branch(repo, branch, "feature.txt", "neu\n")
    assert client.branch_exists(repo.owner, repo.name, branch)
    pr = client.create_pull(repo.owner, repo.name, branch, "main", "[SELF-0001] Selbsttest", "Summary")
    assert pr.number >= 1 and pr.html_url.startswith(GITEA)
    again = client.create_pull(repo.owner, repo.name, branch, "main", "[SELF-0001] Selbsttest", "Summary")
    assert again.number == pr.number  # Duplicate submission finds the existing PR.
    client.comment_pull(repo.owner, repo.name, pr.number, "Erneut eingereicht")

    # Approve → squash merge; branch deleted.
    client.merge_pull(repo.owner, repo.name, pr.number, "squash", title="[SELF-0001] Selbsttest (#1)")
    assert client.file_exists(repo.owner, repo.name, "feature.txt", "main")
    assert not client.branch_exists(repo.owner, repo.name, branch)


def test_live_merge_conflict_raises(client, repo):
    for b, text in (("task/SELF-0002-a", "A\n"), ("task/SELF-0003-b", "B\n")):
        _commit_on_branch(repo, b, "konflikt.txt", text)
    pr_a = client.create_pull(repo.owner, repo.name, "task/SELF-0002-a", "main", "A", "")
    pr_b = client.create_pull(repo.owner, repo.name, "task/SELF-0003-b", "main", "B", "")
    client.merge_pull(repo.owner, repo.name, pr_a.number)
    with pytest.raises(GiteaError):
        client.merge_pull(repo.owner, repo.name, pr_b.number)


@pytest.mark.skipif(not COLLAB, reason="TBR_TEST_GITEA_COLLABORATOR nicht gesetzt")
def test_live_collaborators(client, repo):
    client.set_collaborator(repo.owner, repo.name, COLLAB, "write")
    r = httpx.get(f"{GITEA}/api/v1/repos/{repo.owner}/{repo.name}/collaborators/{COLLAB}/permission",
                  headers={"Authorization": f"token {TOKEN}"}, timeout=15)
    assert r.json()["permission"] == "write"
    client.set_collaborator(repo.owner, repo.name, COLLAB, "read")
    client.remove_collaborator(repo.owner, repo.name, COLLAB)
    client.remove_collaborator(repo.owner, repo.name, COLLAB)  # idempotent (404 ok)
    r = httpx.get(f"{GITEA}/api/v1/repos/{repo.owner}/{repo.name}/collaborators/{COLLAB}",
                  headers={"Authorization": f"token {TOKEN}"}, timeout=15)
    assert r.status_code == 404


def test_live_binary_and_large_attachments(client, repo):
    """Attachments: binary and large files just below 10 MB through the Contents API."""
    import os as _os

    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 16
    big = _os.urandom(9 * 1024 * 1024)
    sha = client.create_files(repo.owner, repo.name, {"tasks/SELF-0009/bild.png": png,
                                                      "tasks/SELF-0009/gross.bin": big},
                              "[SELF-0009] Anhang: bild.png, gross.bin", "main")
    assert sha and len(sha) == 40
    for path, content in (("tasks/SELF-0009/bild.png", png), ("tasks/SELF-0009/gross.bin", big)):
        r = httpx.get(f"{GITEA}/api/v1/repos/{repo.owner}/{repo.name}/raw/{path}", params={"ref": "main"},
                      headers={"Authorization": f"token {TOKEN}"}, timeout=60)
        assert r.status_code == 200 and r.content == content, path
