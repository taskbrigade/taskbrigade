"""brain-changes: maintain project knowledge (brain.md) through conflict-aware change files.

Agents create one `brain-changes/<TASK-NR>.md` per task instead of editing brain.md directly,
so parallel tasks do not collide. TaskBrigade applies change files in approval order:

    ## add: Known pitfalls
    - new entry (appended to the section; a missing section is created)

    ## change: Known pitfalls
    ### old
    - previous entry, exactly as in brain.md
    ### new
    - replacement text

    ## remove: Known pitfalls
    - entry to remove, exactly as in brain.md

`add` never conflicts. `change` and `remove` require the old text to remain unchanged in its section;
otherwise the task returns to the agent for an update against the new state. Text before the first
`## …` heading is ignored.
"""
from app.i18n import _
import re
from dataclasses import dataclass

DIR = "brain-changes"
OPS = ("add", "change", "remove")
_OP_RE = re.compile(r"^##\s+(add|change|remove)\s*:\s*(.+?)\s*$", re.I)
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")


class BrainChangeError(ValueError):
    """Invalid change-file format."""


@dataclass
class Op:
    kind: str
    section: str
    text: str = ""      # add: new text; remove: text to remove.
    old: str = ""       # change
    new: str = ""       # change


def path_for(task_nr: str) -> str:
    return f"{DIR}/{task_nr}.md"


def _block(lines: list[str]) -> str:
    return "\n".join(lines).strip("\n")


def parse(text: str) -> list[Op]:
    ops: list[Op] = []
    current: tuple[str, str, list[str]] | None = None

    def close():
        if current is None:
            return
        kind, section, body = current
        if kind == "change":
            old, new, target = [], [], None
            for line in body:
                head = line.strip().lower()
                if head == "### old":
                    target = old
                elif head == "### new":
                    target = new
                elif target is not None:
                    target.append(line)
                elif line.strip():
                    raise BrainChangeError(_("change ‘%(section)s’: text before ‘### old’ – expected ‘### old’ and ‘### new’", section=section))
            if not _block(old):
                raise BrainChangeError(_("change ‘%(section)s’: ‘### old’ is missing or empty", section=section))
            ops.append(Op("change", section, old=_block(old), new=_block(new)))
        else:
            if not _block(body):
                raise BrainChangeError(_("%(kind)s ‘%(section)s’: no text", kind=kind, section=section))
            ops.append(Op(kind, section, text=_block(body)))

    for line in text.splitlines():
        m = _OP_RE.match(line)
        if m:
            close()
            current = (m.group(1).lower(), m.group(2), [])
        elif _SECTION_RE.match(line) and not line.startswith("###"):
            raise BrainChangeError(_("Unknown line ‘%(line)s’ – expected ‘## add: …’, ‘## change: …’, or ‘## remove: …’", line=line.strip()))
        elif current is not None:
            current[2].append(line)
    close()
    if not ops:
        raise BrainChangeError(_( 'No changes found (expected “## add: <section>”, etc.)' ))
    return ops


# --- Split brain.md into sections ------------------------------------------------------------

def _sections(brain: str) -> list[tuple[str, int, int]]:
    """Return (heading, content start, content end) as character offsets for each `## ` section."""
    heads = [(m.group(1), m.start(), m.end()) for m in re.finditer(r"^##[ \t]+(.+?)[ \t]*$", brain, re.M)]
    out = []
    for i, (name, _start, end) in enumerate(heads):
        content_end = heads[i + 1][1] if i + 1 < len(heads) else len(brain)
        out.append((name, end + 1 if end < len(brain) else end, content_end))
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _find_section(brain: str, name: str) -> tuple[int, int] | None:
    """Find a section by exact name or prefix (e.g. Important decisions matches its ADR variant)."""
    want = _norm(name)
    for heading, start, end in _sections(brain):
        h = _norm(heading)
        if h == want or h.startswith(want + " ") or h.startswith(want + "("):
            return start, end
    return None


def check(brain: str, ops: list[Op]) -> list[str]:
    """Find merge blockers (empty means applicable), in the same order as apply()."""
    try:
        apply(brain, ops)
    except BrainConflict as e:
        return e.issues
    return []


class BrainConflict(Exception):
    def __init__(self, issues: list[str]):
        super().__init__("; ".join(issues))
        self.issues = issues


def apply(brain: str, ops: list[Op]) -> str:
    """Apply changes in order; raise BrainConflict with all problems."""
    issues: list[str] = []
    for op in ops:
        span = _find_section(brain, op.section)
        if op.kind == "add":
            if span is None:
                brain = brain.rstrip("\n") + f"\n\n## {op.section}\n\n{op.text}\n"
                continue
            start, end = span
            body = brain[start:end].rstrip("\n")
            new_body = (body + "\n" if body.strip() else "\n") + op.text + "\n\n"
            if end == len(brain):
                new_body = new_body.rstrip("\n") + "\n"
            brain = brain[:start] + new_body + brain[end:]
            continue
        if span is None:
            issues.append(_("%(kind)s: section ‘%(section)s’ no longer exists in brain.md", kind=op.kind, section=op.section))
            continue
        start, end = span
        body = brain[start:end]
        old = op.old if op.kind == "change" else op.text
        if old not in body:
            issues.append(_("%(kind)s in ‘%(section)s’: the old text no longer matches brain.md – ‘%(text)s…’", kind=op.kind, section=op.section, text=old.splitlines()[0][:80]))
            continue
        replacement = op.new if op.kind == "change" else ""
        body = body.replace(old, replacement, 1)
        if not replacement:  # Avoid a duplicate blank line after removing text.
            body = re.sub(r"\n{3,}", "\n\n", body)
        brain = brain[:start] + body + brain[end:]
    if issues:
        raise BrainConflict(issues)
    return brain
