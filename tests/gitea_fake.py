"""Stateful fake Gitea based on respx for workflow tests."""
import base64
import json
import re
from dataclasses import dataclass, field

import httpx

GITEA = "http://gitea.example.internal:3001"
API = f"{GITEA}/api/v1"
SERVICE_USER = "taskbrigade"


@dataclass
class FakeRepo:
    owner: str
    name: str
    files: dict[str, str] = field(default_factory=dict)
    branches: set[str] = field(default_factory=lambda: {"main"})
    protections: list[dict] = field(default_factory=list)
    collaborators: dict[str, str] = field(default_factory=dict)
    pulls: dict[int, dict] = field(default_factory=dict)
    comments: list[tuple[int, str]] = field(default_factory=list)
    branch_files: dict[str, dict[str, str]] = field(default_factory=dict)  # Files that exist only on a branch.

    def json(self) -> dict:
        return {"owner": {"login": self.owner}, "name": self.name, "default_branch": "main",
                "html_url": f"{GITEA}/{self.owner}/{self.name}",
                "clone_url": f"{GITEA}/{self.owner}/{self.name}.git"}


class FakeGitea:
    def __init__(self, respx_mock):
        self.repos: dict[tuple[str, str], FakeRepo] = {}
        self.merge_fails = False
        # Simulate a merge conflict: {"pr_files": [...], "base_files": [...]} → unmergeable PR, merge 405.
        self.conflict: dict | None = None
        self.pr_files: list[str] | None = None  # PR file list (otherwise derived from `conflict`).
        self.files_fail = False
        self.calls = respx_mock.calls
        m = respx_mock
        R = r"/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)"
        m.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"version": "1.26.1"}))
        m.get(f"{API}/user").mock(return_value=httpx.Response(200, json={"login": SERVICE_USER, "is_admin": False}))
        m.post(f"{API}/user/repos").mock(side_effect=self._create_repo)
        m.post(url__regex=rf"{API}/orgs/(?P<org>[^/]+)/repos$").mock(side_effect=self._create_repo)
        m.get(url__regex=rf"{API}{R}$").mock(side_effect=self._get_repo)
        m.post(url__regex=rf"{API}{R}/contents$").mock(side_effect=self._create_files)
        m.get(url__regex=rf"{API}{R}/contents/(?P<path>[^?]+)").mock(side_effect=self._get_file)
        m.get(url__regex=rf"{API}{R}/raw/(?P<path>[^?]+)").mock(side_effect=self._raw)
        m.get(url__regex=rf"{API}{R}/branch_protections$").mock(side_effect=self._get_prot)
        m.post(url__regex=rf"{API}{R}/branch_protections$").mock(side_effect=self._add_prot)
        m.get(url__regex=rf"{API}{R}/branches/(?P<branch>.+)$").mock(side_effect=self._branch)
        m.post(url__regex=rf"{API}{R}/pulls$").mock(side_effect=self._create_pull)
        m.post(url__regex=rf"{API}{R}/pulls/(?P<num>\d+)/merge$").mock(side_effect=self._merge)
        m.get(url__regex=rf"{API}{R}/pulls/(?P<num>\d+)$").mock(side_effect=self._get_pull)
        m.get(url__regex=rf"{API}{R}/pulls/(?P<num>\d+)/files").mock(side_effect=self._pull_files)
        m.get(url__regex=rf"{API}{R}/compare/(?P<spec>.+)$").mock(side_effect=self._compare)
        m.post(url__regex=rf"{API}{R}/issues/(?P<num>\d+)/comments$").mock(side_effect=self._comment)
        m.put(url__regex=rf"{API}{R}/collaborators/(?P<user>[^/]+)$").mock(side_effect=self._collab_put)
        m.delete(url__regex=rf"{API}{R}/collaborators/(?P<user>[^/]+)$").mock(side_effect=self._collab_del)
        m.get(url__regex=rf"{API}/repos/search").mock(side_effect=self._search)

    # --- Test helpers -------------------------------------------------------------------
    def add_repo(self, owner: str, name: str, files: dict[str, str] | None = None) -> FakeRepo:
        repo = FakeRepo(owner, name, dict(files or {}))
        self.repos[(owner, name)] = repo
        return repo

    def repo(self, owner: str, name: str) -> FakeRepo:
        return self.repos[(owner, name)]

    def push_branch(self, owner: str, name: str, branch: str) -> None:
        self.repo(owner, name).branches.add(branch)

    # --- Handler ----------------------------------------------------------------------------
    def _r(self, owner, repo):
        return self.repos.get((owner, repo))

    def _create_repo(self, request, org=None):
        body = json.loads(request.content)
        owner = org or SERVICE_USER
        if (owner, body["name"]) in self.repos:
            return httpx.Response(409, json={"message": "repo already exists"})
        repo = self.add_repo(owner, body["name"], {"README.md": "# init"} if body.get("auto_init") else {})
        return httpx.Response(201, json=repo.json())

    def _get_repo(self, request, owner, repo):
        r = self._r(owner, repo)
        return httpx.Response(200, json=r.json()) if r else httpx.Response(404, json={"message": "not found"})

    def put_branch_file(self, owner: str, name: str, branch: str, path: str, content: str) -> None:
        self.repo(owner, name).branch_files.setdefault(branch, {})[path] = content

    def _create_files(self, request, owner, repo):
        if self.files_fail:
            return httpx.Response(500, json={"message": "internal error"})
        r = self._r(owner, repo)
        body = json.loads(request.content)
        for f in body["files"]:
            op = f.get("operation", "create")
            if op == "create" and f["path"] in r.files:
                return httpx.Response(422, json={"message": "file exists"})
            if op in ("update", "delete") and (f["path"] not in r.files or not f.get("sha")):
                return httpx.Response(422, json={"message": "sha required / file missing"})
        for f in body["files"]:
            if f.get("operation") == "delete":
                del r.files[f["path"]]
                continue
            raw = base64.b64decode(f["content"])
            try:
                r.files[f["path"]] = raw.decode()
            except UnicodeDecodeError:
                r.files[f["path"]] = raw  # Binary file.
        self.commit_counter = getattr(self, "commit_counter", 0) + 1
        return httpx.Response(201, json={"files": [], "commit": {"sha": f"{self.commit_counter:040x}"}})

    def _get_file(self, request, owner, repo, path):
        r = self._r(owner, repo)
        return (httpx.Response(200, json={"path": path, "sha": f"sha-{abs(hash(path))}"}) if r and path in r.files
                else httpx.Response(404, json={}))

    def _raw(self, request, owner, repo, path):
        r = self._r(owner, repo)
        ref = request.url.params.get("ref")
        if r and ref and path in r.branch_files.get(ref, {}):
            return httpx.Response(200, text=r.branch_files[ref][path])
        return httpx.Response(200, text=r.files[path]) if r and path in r.files else httpx.Response(404, text="")

    def _get_prot(self, request, owner, repo):
        return httpx.Response(200, json=self._r(owner, repo).protections)

    def _add_prot(self, request, owner, repo):
        self._r(owner, repo).protections.append(json.loads(request.content))
        return httpx.Response(201, json={})

    def _branch(self, request, owner, repo, branch):
        r = self._r(owner, repo)
        return httpx.Response(200, json={"name": branch}) if r and branch in r.branches else httpx.Response(404, json={})

    def _create_pull(self, request, owner, repo):
        r = self._r(owner, repo)
        body = json.loads(request.content)
        if any(p["head"] == body["head"] and p["state"] == "open" for p in r.pulls.values()):
            return httpx.Response(409, json={"message": "pull request already exists"})
        num = len(r.pulls) + 1
        r.pulls[num] = {"number": num, "head": body["head"], "base": body["base"], "title": body["title"],
                        "body": body["body"], "state": "open", "merged": False,
                        "html_url": f"{GITEA}/{owner}/{repo}/pulls/{num}"}
        return httpx.Response(201, json=r.pulls[num])

    def _get_pull(self, request, owner, repo, num):
        pr = self._r(owner, repo).pulls[int(num)]
        return httpx.Response(200, json={**pr, "mergeable": self.conflict is None, "merge_base": "abc1234"})

    def _pull_files(self, request, owner, repo, num):
        files = self.pr_files if self.pr_files is not None else (self.conflict or {}).get("pr_files", [])
        return httpx.Response(200, json=[{"filename": f, "status": "modified"} for f in files])

    def _compare(self, request, owner, repo, spec):
        files = (self.conflict or {}).get("base_files", [])
        return httpx.Response(200, json={"total_commits": 1, "commits": [
            {"sha": "def5678", "files": [{"filename": f, "status": "modified"} for f in files]}]})

    def _merge(self, request, owner, repo, num):
        r = self._r(owner, repo)
        if self.conflict is not None:
            return httpx.Response(405, json={"message": "Please try again later"})
        if self.merge_fails:
            return httpx.Response(409, json={"message": "merge conflict"})
        pr = r.pulls[int(num)]
        body = json.loads(request.content)
        pr.update(state="closed", merged=True, merge_style=body["Do"])
        r.files.update(r.branch_files.get(pr["head"], {}))  # Squash: branch contents land on main.
        if body.get("delete_branch_after_merge"):
            r.branches.discard(pr["head"])
        return httpx.Response(200)

    def _comment(self, request, owner, repo, num):
        self._r(owner, repo).comments.append((int(num), json.loads(request.content)["body"]))
        return httpx.Response(201, json={})

    def _collab_put(self, request, owner, repo, user):
        r = self._r(owner, repo)
        r.collaborators[user] = json.loads(request.content)["permission"]
        return httpx.Response(204)

    def _collab_del(self, request, owner, repo, user):
        self._r(owner, repo).collaborators.pop(user, None)
        return httpx.Response(204)

    def _search(self, request):
        q = request.url.params.get("q", "")
        data = [r.json() for r in self.repos.values() if re.search(re.escape(q), r.name)]
        return httpx.Response(200, json={"ok": True, "data": data})
