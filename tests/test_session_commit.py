"""API sessions are committed before the response is sent (FastAPI ≥ 0.121 runs a yield dependency's exit code with
the default scope "request" only after the response) – otherwise `tbr join` → `whoami` saw the old token."""
import asyncio

from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute

from app.api import deps


class FakeSession:
    def __init__(self, events):
        self.events = events

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")


def _call(app, events):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "path": "/x",
             "raw_path": b"/x", "query_string": b"", "headers": [], "client": ("127.0.0.1", 1),
             "server": ("test", 80), "scheme": "http", "root_path": ""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            events.append("response")

    asyncio.run(app(scope, receive, send))


def _app(dependency):
    app = FastAPI()

    @app.post("/x")
    def x(session=dependency):
        return {"ok": True}
    return app


def test_session_dep_commits_before_the_response(monkeypatch):
    events = []
    monkeypatch.setattr(deps.db, "session", lambda: FakeSession(events))
    _call(_app(deps.SessionDep), events)
    assert events.index("commit") < events.index("response")


def test_default_scope_would_commit_after_the_response(monkeypatch):
    """The reason for SessionDep: if FastAPI changes this default again, the comment in deps.py needs an update."""
    events = []
    monkeypatch.setattr(deps.db, "session", lambda: FakeSession(events))
    _call(_app(Depends(deps.get_session)), events)
    assert events.index("response") < events.index("commit")


def _routes(routes):
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        sub = getattr(r, "routes", None) or getattr(getattr(r, "original_router", None), "routes", None)
        if sub:
            yield from _routes(sub)


def _dependants(d):
    yield d
    for sub in d.dependencies:
        yield from _dependants(sub)


def test_every_api_route_uses_the_function_scoped_session():
    from app.main import app

    uses = [(r.path, d.scope) for r in _routes(app.routes) for d in _dependants(r.dependant)
            if d.call is deps.get_session]
    assert len(uses) > 50
    assert [u for u in uses if u[1] != "function"] == []
