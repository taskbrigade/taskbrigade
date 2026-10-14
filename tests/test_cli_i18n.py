"""Agent-facing texts (cli/i18n.py): catalog complete, English default, German via setting, skills valid."""
import ast
import re
import zipfile
from pathlib import Path

import pytest

from cli import client as client_mod
from cli import i18n
from cli import prepare as prep
from cli.i18n import _
from cli.mcp_server import INSTRUCTIONS, TaskBrigadeMCP, build_server
from cli.runner import DEFAULT_PROMPT, RUNNER_GIT_PROMPT

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "cli"
AGENTS = ROOT / "deploy" / "agents"
PLACEHOLDER = re.compile(r"%\(\w+\)[sd]|\{\w+\}")


def _extract() -> dict[str, str]:
    found = {}
    for path in sorted(CLI.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_", "N_")
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                found.setdefault(node.args[0].value, f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


MSGIDS = _extract()
TASK = {"task_nr": "KET-0001", "branch_name": "task/KET-0001-x", "title": "T", "status": "in_progress", "prio": 2,
        "project_info": {"key": "KET", "brain_path": "brain.md", "default_branch": "main"}}


@pytest.fixture
def german(monkeypatch):
    monkeypatch.setenv("TBR_LANGUAGE", "de")


def test_catalog_complete_and_well_formed():
    assert len(MSGIDS) > 150
    cat = i18n.catalog("de")
    missing = sorted(f"{where}: {msgid!r}" for msgid, where in MSGIDS.items() if msgid not in cat)
    assert not missing, "missing in cli/locales/de.json:\n" + "\n".join(missing)
    assert not set(cat) - set(MSGIDS), f"unused entries in cli/locales/de.json: {set(cat) - set(MSGIDS)}"
    bad = [m for m, t in cat.items()
           if not t.strip() or sorted(PLACEHOLDER.findall(m)) != sorted(PLACEHOLDER.findall(t))]
    assert not bad, f"placeholders differ: {bad}"


def test_english_by_default():
    assert i18n.language() == "en"
    md = prep.instructions(TASK, Path("/w"), "tm")
    assert "## How to work on this task" in md and "exactly one" in md
    assert _(INSTRUCTIONS).startswith("TaskBrigade is your team's central task manager")


def test_german_via_environment(german):
    md = prep.instructions(TASK, Path("/w"), "tm")
    assert "## So arbeitest du diesen Task ab" in md and "genau einem" in md
    assert _(DEFAULT_PROMPT).startswith("Du bist der TaskBrigade-Agent {agent}")
    assert "Fälligkeitsdatum" in str(pytest.raises(ValueError, client_mod.parse_due, "morgen").value)


def test_german_via_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv("TBR_LANGUAGE")
    monkeypatch.setattr(i18n, "_configured", None)
    cfg = tmp_path / ".taskbrigade.toml"
    cfg.write_text('url = "http://x"\ntoken = "t"\nlanguage = "de-DE"\n')
    assert client_mod.load_config(cfg).language == "de-DE"
    assert i18n.language() == "de" and _("No task") == "Kein Task"
    monkeypatch.setenv("TBR_LANGUAGE", "en")  # the environment wins over the file
    assert _("No task") == "No task"


def test_mcp_server_texts_follow_language(tmp_path, monkeypatch):
    server = build_server(TaskBrigadeMCP(None, tmp_path))
    assert server.instructions.startswith("TaskBrigade is your team's")
    monkeypatch.setenv("TBR_LANGUAGE", "de")
    assert build_server(TaskBrigadeMCP(None, tmp_path)).instructions.startswith("TaskBrigade ist die zentrale")


RULES = ["tbr submit", "tbr ask", "tbr abort", "tbr comment", "tbr release", "tbr heartbeat", "brain-changes/",
         "brain-changes/README.md", "`## add:", "`## change:`/`## remove:`", "AGENTS.md"]


@pytest.mark.parametrize("git_by_runner", [False, True])
def test_rules_identical_in_both_languages(git_by_runner, monkeypatch):
    """Translation must not change the rules: same commands, files and steps in English and German."""
    texts = {}
    for lang in ("en", "de"):
        monkeypatch.setenv("TBR_LANGUAGE", lang)
        texts[lang] = prep.instructions(TASK, Path("/w"), "tbr", git_by_runner)
    for lang, md in texts.items():
        present = [r for r in RULES if r in md]
        assert present == [r for r in RULES if r in texts["en"]], lang
        assert re.findall(r"^\d\. \*\*", md, re.M) == ["1. **", "2. **", "3. **", "4. **", "5. **"], lang
        assert ("&&" in md) and (("git push" in md) != git_by_runner), lang
    commands = {lang: sorted(re.findall(r"`(tbr [a-z]+|git [a-z]+[^`<]*)", md)) for lang, md in texts.items()}
    assert commands["en"] == commands["de"]


def test_runner_prompts_keep_rules(monkeypatch):
    for lang in ("en", "de"):
        monkeypatch.setenv("TBR_LANGUAGE", lang)
        assert all(x in _(DEFAULT_PROMPT) for x in ("{agent}", "{task_nr}", "TASK.md", "tbr submit", "tbr ask",
                                                     "tbr abort", "&&", "git commit -m"))
        assert all(x in _(RUNNER_GIT_PROMPT) for x in ("{agent}", "{task_nr}", ".tm-submit.md", "tbr ask", "tbr abort"))


# --- skills -----------------------------------------------------------------------------------------

SKILLS = sorted(AGENTS.glob("*/taskbrigade/SKILL.md"))


def _frontmatter(text: str) -> dict:
    yaml = pytest.importorskip("yaml")
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md must start with a YAML frontmatter block"
    return yaml.safe_load(m.group(1))


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.parent.name)
def test_skill_frontmatter_valid(path):
    meta = _frontmatter(path.read_text())
    assert meta["name"] == "taskbrigade"
    assert isinstance(meta["description"], str) and len(meta["description"]) > 40
    assert "tbr submit" in path.read_text() or "submit_task" in path.read_text()


def test_skills_found():
    assert {p.parent.parent.name for p in SKILLS} == {"cowork", "hermes", "openclaw"}


def test_cowork_zip_matches_skill():
    with zipfile.ZipFile(AGENTS / "cowork" / "taskbrigade-skill.zip") as z:
        assert z.namelist() == ["taskbrigade/SKILL.md"]
        packed = z.read("taskbrigade/SKILL.md").decode()
    assert packed == (AGENTS / "cowork" / "taskbrigade" / "SKILL.md").read_text()
    assert _frontmatter(packed)["name"] == "taskbrigade"
