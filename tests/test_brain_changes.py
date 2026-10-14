"""brain-changes: parsing and application without a DB."""
import pytest

from app.services import brain_changes as bc

BRAIN = """# Demo – Brain

## Purpose

Demo project.

## Important decisions (ADR short form: date – decision – reason)

- 2026-10-01 – Use MariaDB – team knows it.

## Known pitfalls

- Tests need a `*_test` database.
- Old pitfall that will be changed.

## Glossary

- Lease: time-limited claim.
"""


def test_parse_all_ops():
    ops = bc.parse("""# Brain changes for DEMO-0001

## add: Known pitfalls
- New pitfall
  with two lines.

## change: Known pitfalls
### old
- Old pitfall that will be changed.
### new
- Changed pitfall.

## remove: Glossary
- Lease: time-limited claim.
""")
    assert [(o.kind, o.section) for o in ops] == [("add", "Known pitfalls"), ("change", "Known pitfalls"),
                                                 ("remove", "Glossary")]
    assert ops[0].text == "- New pitfall\n  with two lines."
    assert ops[1].old == "- Old pitfall that will be changed." and ops[1].new == "- Changed pitfall."


@pytest.mark.parametrize("text,msg", [
    ("", "Keine Änderungen"),
    ("## update: Known pitfalls\n- x", "Unbekannte Zeile"),
    ("## add: Known pitfalls\n", "kein Text"),
    ("## change: Known pitfalls\n- x\n", "### old"),
    ("## change: Known pitfalls\n### new\n- y\n", "### old"),
])
def test_parse_errors(text, msg):
    with pytest.raises(bc.BrainChangeError, match=msg):
        bc.parse(text)


def test_apply_add_change_remove():
    ops = bc.parse("## add: Known pitfalls\n- New pitfall.\n\n"
                   "## change: Known pitfalls\n### old\n- Old pitfall that will be changed.\n### new\n- Changed.\n\n"
                   "## add: Important decisions\n- 2026-10-08 – brain-changes – no merge conflicts.\n\n"
                   "## remove: Glossary\n- Lease: time-limited claim.\n\n"
                   "## add: Open questions\n- Is this needed?\n")
    out = bc.apply(BRAIN, ops)
    assert "- Tests need a `*_test` database.\n- Changed.\n- New pitfall.\n\n## Glossary" in out
    assert "- 2026-10-01 – Use MariaDB – team knows it.\n- 2026-10-08 – brain-changes" in out  # Prefix section name.
    assert "Lease" not in out
    assert out.endswith("## Open questions\n\n- Is this needed?\n")  # Missing section is created.
    assert "\n\n\n" not in out


def test_adds_from_parallel_tasks_never_conflict():
    a = bc.parse("## add: Known pitfalls\n- From task A.\n")
    b = bc.parse("## add: Known pitfalls\n- From task B.\n")
    out = bc.apply(bc.apply(BRAIN, a), b)
    assert "- From task A.\n- From task B.\n\n## Glossary" in out


def test_change_on_outdated_text_is_a_conflict():
    first = bc.parse("## change: Known pitfalls\n### old\n- Old pitfall that will be changed.\n### new\n- A.\n")
    second = bc.parse("## change: Known pitfalls\n### old\n- Old pitfall that will be changed.\n### new\n- B.\n"
                      "## remove: Missing section\n- x\n")
    brain = bc.apply(BRAIN, first)  # Task 1 approved first.
    issues = bc.check(brain, second)
    assert len(issues) == 2 and "steht so nicht (mehr)" in issues[0] and "Missing section" in issues[1]
    with pytest.raises(bc.BrainConflict):
        bc.apply(brain, second)
    assert bc.check(BRAIN, second[:1]) == []  # It would have applied to the old state.


def test_add_to_last_section_keeps_single_trailing_newline():
    out = bc.apply(BRAIN, bc.parse("## add: Glossary\n- Owner: responsible human.\n"))
    assert out.endswith("- Lease: time-limited claim.\n- Owner: responsible human.\n")
