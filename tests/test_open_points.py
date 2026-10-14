"""Open points without a database: parsing `.tm-submit.md`, server-side cleaning, limit note for empty claims."""
import pytest

from app.errors import Unprocessable
from app.services.state_machine import clean_open_points
from cli.client import ApiError, parallel_limit_note, split_open_points


def test_split_open_points_from_submit_file():
    text = ("Login implemented, tests added.\n\n## Open points\n\n- Rate limit missing\n"
            "- [ ] Docs: describe the\n  new option\n* 3rd point\n1. numbered\n")
    summary, points = split_open_points(text)
    assert summary == "Login implemented, tests added."
    assert points == ["Rate limit missing", "Docs: describe the new option", "3rd point", "numbered"]
    assert split_open_points("Only a summary\n- no heading") == ("Only a summary\n- no heading", [])
    assert split_open_points("x\n### Offene Punkte:\n- a")[1] == ["a"]


def test_split_open_points_keeps_text_after_the_list():
    summary, points = split_open_points("Done.\n## Open points\n- a\n\nTested with pytest.")
    assert points == ["a"] and summary == "Done.\n\nTested with pytest."


def test_clean_open_points():
    assert clean_open_points(None) == []
    assert clean_open_points(["- a", "", "  b ", "1) c", "- [x] d"]) == ["a", "b", "c", "d"]
    with pytest.raises(Unprocessable):
        clean_open_points("a")
    with pytest.raises(Unprocessable):
        clean_open_points(["a"] * 51)


class FakeClient:
    def __init__(self, limit, running, fail=False):
        self.limit, self.running, self.fail = limit, running, fail

    def get(self, path, **kw):
        if self.fail:
            raise ApiError(500, "x", "down")
        if path == "/agents/me":
            return {"max_parallel_tasks": self.limit}
        assert kw["params"] == {"status": "in_progress"}
        return {"items": [{"task_nr": nr} for nr in self.running]}


def test_parallel_limit_note():
    note = parallel_limit_note(FakeClient(2, ["KET-0001", "KET-0002"]))
    assert "Limit reached" in note and "KET-0001, KET-0002" in note and " 2 " in note
    assert parallel_limit_note(FakeClient(2, ["KET-0001"])) is None
    assert parallel_limit_note(FakeClient(2, [], fail=True)) is None
