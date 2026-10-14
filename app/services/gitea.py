"""Gitea REST-API v1 (httpx)."""
from app.i18n import _
import base64
from dataclasses import dataclass
from urllib.parse import quote

import httpx

TIMEOUT = httpx.Timeout(15.0)


class GiteaError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class GiteaUser:
    login: str
    email: str | None
    is_admin: bool


@dataclass
class Repo:
    owner: str
    name: str
    html_url: str
    clone_url: str
    default_branch: str


@dataclass
class PullRequest:
    number: int
    html_url: str
    state: str
    merged: bool


def _message(r: httpx.Response) -> str:
    try:
        return str(r.json().get("message") or r.text[:200])
    except ValueError:
        return r.text[:200]


class GiteaClient:
    def __init__(self, base_url: str, token: str | None = None):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token

    def _request(self, method: str, path: str, token: str | None = None, **kw) -> httpx.Response:
        if not self.base_url:
            raise GiteaError(_("Gitea URL is not configured"))
        headers = {"Accept": "application/json"}
        tok = token or self.token
        if tok:
            headers["Authorization"] = f"token {tok}"
        try:
            return httpx.request(method, f"{self.base_url}/api/v1{path}", headers=headers, timeout=TIMEOUT, **kw)
        except httpx.HTTPError as e:
            raise GiteaError(_("Gitea unreachable: %(error)s", error=type(e).__name__)) from e

    def _json(self, method: str, path: str, ok: tuple[int, ...] = (200, 201), **kw):
        r = self._request(method, path, **kw)
        if r.status_code not in ok:
            raise GiteaError(f"Gitea {method} {path}: HTTP {r.status_code} – {_message(r)}", r.status_code)
        try:
            return r.json() if r.content else None
        except ValueError as e:
            raise GiteaError(_("Unexpected response from Gitea (is this a Gitea URL?)")) from e

    def _require_token(self) -> None:
        if not self.token:
            raise GiteaError(_("No Gitea service token configured (Admin → Settings)"))

    @staticmethod
    def _repo_path(owner: str, repo: str) -> str:
        return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"

    # --- Users ---------------------------------------------------------------------------

    def version(self) -> str:
        return (self._json("GET", "/version") or {}).get("version", "?")

    def current_user(self, token: str | None = None) -> GiteaUser:
        """Get the token's user (`GET /user`). 401 means an invalid token."""
        r = self._request("GET", "/user", token=token)
        if r.status_code == 401:
            raise GiteaError(_( 'Token rejected by Gitea (401)' ), 401)
        if r.status_code != 200:
            raise GiteaError(_("Gitea responded with HTTP %(status)d", status=r.status_code), r.status_code)
        try:
            d = r.json()
            return GiteaUser(d["login"], d.get("email"), bool(d.get("is_admin")))
        except (ValueError, KeyError, TypeError) as e:
            raise GiteaError(_("Unexpected response from Gitea (is this a Gitea URL?)")) from e

    # --- Agent users (admin token, app/services/onboarding.py) --------------------------------

    def user_exists(self, username: str) -> bool:
        r = self._request("GET", f"/users/{quote(username, safe='')}")
        if r.status_code == 404:
            return False
        if r.status_code != 200:
            raise GiteaError(_("Gitea user %(user)s: HTTP %(status)d – %(message)s", user=username,
                               status=r.status_code, message=_message(r)), r.status_code)
        return True

    def admin_create_user(self, username: str, email: str, password: str) -> None:
        """`POST /admin/users` – needs a site admin token with scope `write:admin`."""
        self._require_token()
        self._json("POST", "/admin/users", ok=(201,), json={
            "username": username, "email": email, "password": password, "must_change_password": False})

    def admin_delete_user(self, username: str) -> None:
        """Best effort (cleanup after a half-finished setup); errors are ignored."""
        try:
            self._request("DELETE", f"/admin/users/{quote(username, safe='')}")
        except GiteaError:
            pass

    def create_token_as(self, username: str, password: str, name: str, scopes: list[str]) -> str:
        """Create a token for `username` with its own basic auth: Gitea only accepts basic auth for token creation,
        so an admin token cannot do this for other users."""
        path = f"/users/{quote(username, safe='')}/tokens"
        try:
            r = httpx.post(f"{self.base_url}/api/v1{path}", auth=(username, password), timeout=TIMEOUT,
                           headers={"Accept": "application/json"}, json={"name": name, "scopes": scopes})
        except httpx.HTTPError as e:
            raise GiteaError(_("Gitea unreachable: %(error)s", error=type(e).__name__)) from e
        if r.status_code != 201:
            raise GiteaError(_("Create Gitea token for %(user)s: HTTP %(status)d – %(message)s", user=username,
                               status=r.status_code, message=_message(r)), r.status_code)
        try:
            return r.json()["sha1"]
        except (ValueError, KeyError, TypeError) as e:
            raise GiteaError(_("Unexpected response from Gitea (is this a Gitea URL?)")) from e

    # --- Repos ------------------------------------------------------------------------------

    @staticmethod
    def _repo(d: dict) -> Repo:
        return Repo(d["owner"]["login"], d["name"], d["html_url"], d["clone_url"], d.get("default_branch") or "main")

    def get_repo(self, owner: str, repo: str) -> Repo | None:
        r = self._request("GET", self._repo_path(owner, repo))
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise GiteaError(_("Repository %(owner)s/%(repo)s: HTTP %(status)d – %(message)s", owner=owner, repo=repo, status=r.status_code, message=_message(r)), r.status_code)
        return self._repo(r.json())

    def create_repo(self, owner: str, name: str, description: str = "", default_branch: str = "main") -> Repo:
        self._require_token()
        body = {"name": name, "description": description, "private": True, "auto_init": True,
                "default_branch": default_branch}
        me = self.current_user().login
        path = "/user/repos" if owner.lower() == me.lower() else f"/orgs/{quote(owner, safe='')}/repos"
        return self._repo(self._json("POST", path, json=body))

    def search_repos(self, query: str = "", limit: int = 50) -> list[Repo]:
        d = self._json("GET", "/repos/search", params={"q": query, "limit": limit})
        return [self._repo(x) for x in (d or {}).get("data", [])]

    def file_exists(self, owner: str, repo: str, path: str, ref: str | None = None) -> bool:
        r = self._request("GET", f"{self._repo_path(owner, repo)}/contents/{quote(path)}",
                          params={"ref": ref} if ref else None)
        if r.status_code == 404:
            return False
        if r.status_code != 200:
            raise GiteaError(_("File %(path)s: HTTP %(status)d – %(message)s", path=path, status=r.status_code, message=_message(r)), r.status_code)
        return True

    def raw_file(self, owner: str, repo: str, path: str, ref: str) -> str | None:
        r = self._request("GET", f"{self._repo_path(owner, repo)}/raw/{quote(path)}", params={"ref": ref})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise GiteaError(_("File %(path)s: HTTP %(status)d – %(message)s", path=path, status=r.status_code, message=_message(r)), r.status_code)
        return r.text

    def create_files(self, owner: str, repo: str, files: dict[str, str | bytes], message: str, branch: str) -> str | None:
        """Add multiple text or binary files in one commit (`POST /repos/{o}/{r}/contents`, Gitea ≥ 1.20).
        Return the commit SHA if Gitea provides it."""
        self._require_token()
        body = {"branch": branch, "message": message, "files": [
            {"operation": "create", "path": path,
             "content": base64.b64encode(content if isinstance(content, bytes) else content.encode()).decode()}
            for path, content in files.items()]}
        result = self._json("POST", f"{self._repo_path(owner, repo)}/contents", json=body) or {}
        return (result.get("commit") or {}).get("sha")

    def file_sha(self, owner: str, repo: str, path: str, ref: str) -> str | None:
        r = self._request("GET", f"{self._repo_path(owner, repo)}/contents/{quote(path)}", params={"ref": ref})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise GiteaError(_("File %(path)s: HTTP %(status)d – %(message)s", path=path, status=r.status_code, message=_message(r)), r.status_code)
        return (r.json() or {}).get("sha")

    def change_files(self, owner: str, repo: str, updates: dict[str, str], deletes: list[str], message: str,
                     branch: str) -> str | None:
        """Change or delete existing files in one commit (`POST /repos/{o}/{r}/contents`, Gitea ≥ 1.20)."""
        self._require_token()
        files = []
        for path, content in updates.items():
            sha = self.file_sha(owner, repo, path, branch)
            op = {"operation": "update" if sha else "create", "path": path,
                  "content": base64.b64encode(content.encode()).decode()}
            if sha:
                op["sha"] = sha
            files.append(op)
        for path in deletes:
            sha = self.file_sha(owner, repo, path, branch)
            if sha:
                files.append({"operation": "delete", "path": path, "sha": sha})
        if not files:
            return None
        result = self._json("POST", f"{self._repo_path(owner, repo)}/contents",
                            json={"branch": branch, "message": message, "files": files}) or {}
        return (result.get("commit") or {}).get("sha")

    def pull_files(self, owner: str, repo: str, number: int) -> list[str]:
        self._require_token()
        return [f["filename"] for f in self._json("GET", f"{self._repo_path(owner, repo)}/pulls/{number}/files",
                                                  params={"limit": 200}) or []]

    def protect_branch(self, owner: str, repo: str, branch: str, service_user: str) -> None:
        """Allow pushes only by the service user and merges only through PRs by that user."""
        self._require_token()
        path = f"{self._repo_path(owner, repo)}/branch_protections"
        existing = self._json("GET", path) or []
        if any((p.get("rule_name") or p.get("branch_name")) == branch for p in existing):
            return
        self._json("POST", path, json={
            "rule_name": branch, "branch_name": branch,
            "enable_push": True, "enable_push_whitelist": True, "push_whitelist_usernames": [service_user],
            "enable_merge_whitelist": True, "merge_whitelist_usernames": [service_user],
        })

    # --- Branches / PRs ---------------------------------------------------------------------

    def branch_exists(self, owner: str, repo: str, branch: str) -> bool:
        r = self._request("GET", f"{self._repo_path(owner, repo)}/branches/{quote(branch, safe='/')}")
        if r.status_code == 404:
            return False
        if r.status_code != 200:
            raise GiteaError(_("Branch %(branch)s: HTTP %(status)d – %(message)s", branch=branch, status=r.status_code, message=_message(r)), r.status_code)
        return True

    @staticmethod
    def _pr(d: dict) -> PullRequest:
        return PullRequest(d["number"], d["html_url"], d.get("state", "open"), bool(d.get("merged")))

    def create_pull(self, owner: str, repo: str, head: str, base: str, title: str, body: str) -> PullRequest:
        self._require_token()
        r = self._request("POST", f"{self._repo_path(owner, repo)}/pulls",
                          json={"head": head, "base": base, "title": title, "body": body})
        if r.status_code == 409:  # PR already exists (e.g. after an interrupted submission).
            existing = self._json("GET", f"{self._repo_path(owner, repo)}/pulls/{quote(base, safe='')}/"
                                         f"{quote(head, safe='/')}")
            return self._pr(existing)
        if r.status_code != 201:
            raise GiteaError(_("Create pull request: HTTP %(status)d – %(message)s", status=r.status_code, message=_message(r)), r.status_code)
        return self._pr(r.json())

    def comment_pull(self, owner: str, repo: str, number: int, body: str) -> None:
        self._require_token()
        self._json("POST", f"{self._repo_path(owner, repo)}/issues/{number}/comments", json={"body": body})

    def merge_pull(self, owner: str, repo: str, number: int, style: str = "squash", title: str | None = None,
                   delete_branch: bool = True) -> None:
        """Raise GiteaError with status 405/409 for merge conflicts or nonmergeable PRs."""
        self._require_token()
        body = {"Do": style, "delete_branch_after_merge": delete_branch}
        if title:
            body["MergeTitleField"] = title
        r = self._request("POST", f"{self._repo_path(owner, repo)}/pulls/{number}/merge", json=body)
        if r.status_code not in (200, 204):
            raise GiteaError(_("Merge failed: HTTP %(status)d – %(message)s", status=r.status_code, message=_message(r)), r.status_code)

    def pull_conflicts(self, owner: str, repo: str, number: int, base: str) -> list[str] | None:
        """None means the PR is mergeable. Otherwise return files changed on both task and base branches
        since divergence: conflict candidates, as Gitea does not name conflicting files directly."""
        self._require_token()
        path = self._repo_path(owner, repo)
        pr = self._json("GET", f"{path}/pulls/{number}")
        if pr.get("mergeable", True):
            return None
        pr_files = {f["filename"] for f in self._json("GET", f"{path}/pulls/{number}/files",
                                                     params={"limit": 200}) or []}
        base_files: set[str] = set()
        if pr.get("merge_base"):
            cmp = self._json("GET", f"{path}/compare/{pr['merge_base']}...{quote(base, safe='')}") or {}
            for commit in cmp.get("commits") or []:
                base_files |= {f["filename"] for f in commit.get("files") or []}
        return sorted(pr_files & base_files)

    # --- Collaborators ----------------------------------------------------------------------

    def set_collaborator(self, owner: str, repo: str, username: str, permission: str) -> None:
        self._require_token()
        self._json("PUT", f"{self._repo_path(owner, repo)}/collaborators/{quote(username, safe='')}",
                   ok=(204, 200), json={"permission": permission})

    def remove_collaborator(self, owner: str, repo: str, username: str) -> None:
        self._require_token()
        r = self._request("DELETE", f"{self._repo_path(owner, repo)}/collaborators/{quote(username, safe='')}")
        if r.status_code not in (204, 404):
            raise GiteaError(_("Remove collaborator: HTTP %(status)d – %(message)s", status=r.status_code, message=_message(r)), r.status_code)
