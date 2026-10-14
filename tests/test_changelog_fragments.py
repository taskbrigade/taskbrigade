"""changelog.d: inserting fragments into CHANGELOG.md (no DB)."""
import pytest

from app.services import changelog_fragments as cf

CHANGELOG = "# Changelog\n\nAll notable changes.\n\n## 0.4.0 – 2026-10-07\n\n- Owner.\n"


def test_creates_unreleased_below_title_and_appends_in_order():
    once = cf.insert(CHANGELOG, "- First.")
    assert once == "# Changelog\n\nAll notable changes.\n\n## Unreleased\n\n- First.\n\n## 0.4.0 – 2026-10-07\n\n- Owner.\n"
    twice = cf.insert(once, "- Second\n  with two lines.")
    assert "## Unreleased\n\n- First.\n- Second\n  with two lines.\n\n## 0.4.0" in twice


def test_existing_unreleased_at_end_and_empty_file():
    assert cf.insert("# Changelog\n\n## Unreleased\n", "- X.") == "# Changelog\n\n## Unreleased\n\n- X.\n"
    assert cf.insert("", "- X.") == "# Changelog\n\n## Unreleased\n\n- X.\n"


@pytest.mark.parametrize("text", ["", "  \n\n", "## Added\n- x"])
def test_invalid_fragments(text):
    with pytest.raises(ValueError):
        cf.clean(text)
