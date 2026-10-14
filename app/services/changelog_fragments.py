"""changelog.d: CHANGELOG.md entries without merge conflicts.

Agents do not edit CHANGELOG.md directly; they add `changelog.d/<TASK-NR>.md` with Markdown bullet points. On
approval TaskBrigade appends the fragment to the `## Unreleased` section of CHANGELOG.md (creating the section right
below the title if needed) and deletes the fragment – in the same commit as the brain-changes.
"""
import re

DIR = "changelog.d"
CHANGELOG = "CHANGELOG.md"
SECTION = "Unreleased"


def path_for(task_nr: str) -> str:
    return f"{DIR}/{task_nr}.md"


def clean(text: str) -> str:
    """Fragment text without leading/trailing blank lines; raises ValueError if empty or if it contains headings."""
    body = text.strip("\n").rstrip()
    if not body.strip():
        raise ValueError("fragment is empty")
    if re.search(r"^#{1,2}\s", body, re.M):
        raise ValueError("fragment must not contain `#`/`##` headings – bullet points only")
    return body


def insert(changelog: str, entry: str) -> str:
    """Append `entry` at the end of `## Unreleased`; create the section below the title if it is missing."""
    heads = list(re.finditer(r"^##[ \t]+(.+?)[ \t]*$", changelog, re.M))
    for i, m in enumerate(heads):
        if m.group(1).strip().lower().startswith(SECTION.lower()):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(changelog)
            body = changelog[m.end():end].rstrip("\n")
            tail = "\n\n" if i + 1 < len(heads) else "\n"
            return changelog[:m.end()] + (body + "\n" if body.strip() else "\n\n") + entry + tail + changelog[end:]
    if not changelog.strip():
        return f"# Changelog\n\n## {SECTION}\n\n{entry}\n"
    title = re.match(r"^#[ \t]+.*\n+(?:(?!##)[^\n]*\n+)*", changelog)  # title plus intro text up to the first ##
    cut = title.end() if title else 0
    return changelog[:cut].rstrip("\n") + f"\n\n## {SECTION}\n\n{entry}\n\n" + changelog[cut:].lstrip("\n")
