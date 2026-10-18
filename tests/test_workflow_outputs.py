"""WF 3: placeholder resolver, publish checks, and the §3.7 content pipeline at resolver level."""

import json
from datetime import UTC, datetime

import pytest

from app.services.workflows.contracts import (
    ContractError, build_output_json, parse_output_contract, retarget_file_outputs, resolve_bindings,
    validate_outputs,
)
from app.services.workflows.placeholders import (
    SHORT_SHA, Context, PlaceholderError, StepValue, failure_effect, render,
)
from app.services.workflows.publish import blocking, check_publish
from cli.outputs import parse_output_args, required_outputs_lines, split_submit_sections
from cli.tm import task_markdown

COMMIT = "3f2a" + "1" * 36
MERGE = "9b1c" + "2" * 36
FILE_COMMIT = "4d7e" + "3" * 36
KERN = "1. Vorlagen sind versioniert …"


def pipeline(**overrides):
    steps = [
        {"key": "recherche", "type": "agent", "role": "researcher", "data_class": "cloud_ok", "review": "auto",
         "title": "Recherche: {{thema}}",
         "outputs": {"quellen": {"type": "json", "required": True, "schema": {"type": "array", "items": {"type": "string"}}},
                     "kernaussagen": {"type": "text", "required": True, "max_length": 4000}}},
        {"key": "entwurf", "type": "agent", "role": "writer", "review": "agent",
         "title": "Entwurf: {{thema}} für {{zielgruppe}}",
         "description": "Kernaussagen aus {{steps.recherche.task_nr}}:\n{{steps.recherche.output.kernaussagen}}",
         "outputs": {"datei": {"type": "file", "required": True}}},
        {"key": "freigabe", "type": "approval", "reviewer": "human:owner", "title": "Freigabe Beitrag {{thema}}"},
        {"key": "info", "type": "notify",
         "config": {"to": ["owner"], "text": "Beitrag {{thema}} freigegeben: {{steps.entwurf.output.datei}}"}},
    ]
    definition = {
        "key": "content-pipeline", "name": "Content pipeline", "version": 3,
        "project_data_class": "cloud_ok",
        "params": [
            {"name": "thema", "type": "string", "label": "Thema"},
            {"name": "zielgruppe", "type": "enum", "values": ["kunden", "partner"], "default": "kunden"},
        ],
        "steps": steps,
    }
    definition.update(overrides)
    return definition


def recherche_step():
    return StepValue(
        step_type="agent", data_class="cloud_ok", status="done", task_nr="DEMO-0201",
        summary="12 Quellen ausgewertet", branch="task/DEMO-0201-recherche-wiederholbare-workflows",
        pr=57, commit=COMMIT, merge_commit=MERGE, open_points=[],
        output={"kernaussagen": KERN, "quellen": ["https://example.org/a", "https://example.org/b"]})


def test_content_pipeline_example():
    """§3.7: publish, resolve, validate, reject a missing file, retarget it onto the merge commit."""
    definition = pipeline()
    assert blocking(check_publish(definition, role_candidates={"researcher": 1, "writer": 1})) == []

    ctx = Context(params={"thema": "Wiederholbare Workflows", "zielgruppe": "kunden"},
                  workflow_key="content-pipeline", workflow_name="Content pipeline", workflow_version=3,
                  project_key="DEMO", project_name="Demo")
    assert render("Recherche: {{thema}}", ctx).text == "Recherche: Wiederholbare Workflows"

    contract = parse_output_contract(definition["steps"][0]["outputs"])
    declared = validate_outputs(contract, {
        "kernaussagen": KERN,
        "quellen": ["https://example.org/a", "https://example.org/b"],
    })
    stored = build_output_json(
        task_nr="DEMO-0201", status="done", summary="12 Quellen ausgewertet",
        branch="task/DEMO-0201-recherche-wiederholbare-workflows", commit=COMMIT, merge_commit=MERGE,
        open_points=[], declared=declared, pr=57)
    assert stored["output"]["kernaussagen"] == KERN
    assert stored["output"]["quellen"] == ["https://example.org/a", "https://example.org/b"]
    assert stored["task_nr"] == "DEMO-0201" and stored["pr"] == 57 and stored["merge_commit"] == MERGE

    described = render(definition["steps"][1]["description"], Context(
        params=ctx.params, steps={"recherche": recherche_step()})).text
    assert described == f"Kernaussagen aus DEMO-0201:\n{KERN}"

    file_contract = parse_output_contract(definition["steps"][1]["outputs"])
    with pytest.raises(ContractError) as missing:
        validate_outputs(file_contract, {})
    assert missing.value.code == "outputs_invalid"
    # The suite sets UI_LANGUAGE=de, so the text is the German catalog entry of the English msgid.
    assert str(missing.value) in {"datei (file) is missing", "datei (file) fehlt"}

    def exists(path, commit):
        return path == "content/workflows.md" and commit in {COMMIT, FILE_COMMIT}

    filed = validate_outputs(file_contract, {"datei": "content/workflows.md"}, commit=COMMIT, file_exists=exists)
    assert filed == {"datei": {"path": "content/workflows.md", "commit": COMMIT}}
    merged = retarget_file_outputs(filed, file_contract, FILE_COMMIT, exists)
    assert merged["datei"] == {"path": "content/workflows.md", "commit": FILE_COMMIT}

    info = render(definition["steps"][3]["config"]["text"], Context(
        params=ctx.params,
        steps={"entwurf": StepValue(step_type="agent", data_class="cloud_ok", status="done",
                                    output={"datei": merged["datei"]})})).text
    assert info == f"Beitrag Wiederholbare Workflows freigegeben: content/workflows.md@{FILE_COMMIT[:SHORT_SHA]}"


def test_publish_rejects_the_two_counterexamples():
    bad = pipeline()
    bad["steps"][1]["description"] = "{{steps.recherche.output.zusammenfassung}}"
    codes = {issue.code for issue in check_publish(bad)}
    assert "undeclared_output" in codes

    zoned = pipeline()
    zoned["steps"][0]["data_class"] = "local_only"
    zoned["steps"][1]["data_class"] = "cloud_ok"
    zone_issues = [issue for issue in check_publish(zoned) if issue.code == "zone_boundary_without_gate"]
    assert zone_issues and "manual approval" in zone_issues[0].message

    zoned["steps"].insert(1, {"key": "freigeben", "type": "approval", "review": "manual", "reviewer": "human:owner",
                              "title": "Freigabe der Recherche"})
    assert not [issue for issue in check_publish(zoned) if issue.code == "zone_boundary_without_gate"]


def test_resolver_syntax_and_strict_values():
    ctx = Context(params={"thema": "Workflows", "leer": "", "anzahl": 0})
    assert render(r"\{{thema}} bleibt", ctx).text == "{{thema}} bleibt"
    assert render('{{leer | default("nichts")}}', ctx).text == "nichts"
    assert render('{{fehlt | default("x")}}', ctx).text == "x"
    assert render("{{anzahl | default(\"9\")}}", ctx).text == "0"
    with pytest.raises(PlaceholderError) as empty:
        render("{{leer}}", ctx)
    assert empty.value.code == "placeholder_unresolved" and empty.value.path == "leer"
    with pytest.raises(PlaceholderError) as syntax:
        render("{{thema | nope}}", ctx)
    assert syntax.value.code == "placeholder_syntax"
    with pytest.raises(PlaceholderError):
        render("Hallo {{thema", ctx)
    with pytest.raises(PlaceholderError) as unfinished:
        render("{{steps.spater.task_nr}}", ctx)
    assert unfinished.value.code == "placeholder_unresolved"
    assert "unknown step" in str(unfinished.value)
    effect = failure_effect(unfinished.value)
    assert effect["run_step_status"] == "failed" and effect["run_status"] == "paused"
    assert effect["error_code"] == "placeholder_unresolved"

    masked = render("{{steps.recherche.output.kernaussagen}}", Context(steps={
        "recherche": StepValue(step_type="agent", data_class="local_only", status="done", output={"kernaussagen": KERN}),
    }))
    assert masked.text == KERN and masked.trace[0]["masked"] and masked.trace[0]["value"] == "***"


def test_run_period_uses_the_schedule_timezone():
    ctx = Context(scheduled_for=datetime(2026, 3, 1, 0, 30), timezone="America/New_York")
    assert render("{{run.this_month}}", ctx).text == "2026-02"
    assert render("{{run.period}}", ctx).text == render("{{run.prev_month}}", ctx).text == "2026-01"
    assert render("{{run.year}}", ctx).text == "2026"
    january = Context(scheduled_for=datetime(2026, 1, 15, tzinfo=UTC), timezone="UTC")
    assert render("{{run.period}}", january).text == "2025-12"
    assert render('{{run.this_month | date("%Y/%m")}}', january).text == "2026/01"


def test_publish_checks_cover_section_3_6():
    lone = {"steps": [{"key": "schreiben", "type": "agent", "title": "Nur {{monat}}"}]}
    assert any(issue.code == "unknown_placeholder" for issue in check_publish(lone))

    later = {"steps": [
        {"key": "a", "type": "agent", "role": "writer", "title": "{{steps.b.task_nr}}"},
        {"key": "b", "type": "agent", "role": "writer", "title": "B"},
    ]}
    assert any(issue.code == "unknown_placeholder" for issue in check_publish(later))

    filtered = {"params": [{"name": "anzahl", "type": "int"}],
                "steps": [{"key": "a", "type": "agent", "role": "writer", "title": "{{anzahl | upper}}"}]}
    assert any(issue.code == "filter_mismatch" for issue in check_publish(filtered))

    assert any(issue.code == "no_assignee_rule" for issue in check_publish(
        {"steps": [{"key": "a", "type": "agent", "title": "Offen"}]}))

    script = {"steps": [{"key": "code", "type": "script", "role": "coder", "review": "auto", "title": "Skript"}]}
    assert any(issue.code == "auto_review_not_allowed" for issue in check_publish(script, data_enabled=True))
    assert any(issue.code == "runner_without_data_zones" for issue in check_publish(
        {"steps": [{"key": "lauf", "type": "runner", "title": "Lauf"}]}))

    warned = check_publish({"steps": [{"key": "a", "type": "agent", "role": "writer", "title": "A"}]},
                           role_candidates={"writer": 0})
    assert any(issue.code == "role_unresolvable" and issue.severity == "warning" for issue in warned)
    assert blocking(warned) == []

    assert any(issue.code == "param_default_invalid" for issue in check_publish(
        {"params": [{"name": "monat", "type": "month", "default": "2026-13"}],
         "steps": [{"key": "a", "type": "notify", "title": "{{monat}}"}]}))
    assert blocking(check_publish(
        {"params": [{"name": "monat", "type": "month", "default": "{{run.prev_month}}"}],
         "steps": [{"key": "a", "type": "notify", "title": "{{monat}}"}]})) == []
    assert any(issue.code == "param_default_invalid" for issue in check_publish(
        {"params": [{"name": "monat", "type": "month", "default": "{{run.year}}"}],
         "steps": [{"key": "a", "type": "notify", "title": "x"}]}))

    sealed = {"data_enabled": False, "steps": [
        {"key": "lauf", "type": "runner", "data_class": "sealed", "title": "Profil"},
        {"key": "text", "type": "agent", "role": "writer", "data_class": "cloud_ok",
         "title": "{{steps.lauf.summary}}"},
    ]}
    assert any(issue.code == "zone_boundary_without_gate" and "sealed values" in issue.message
               for issue in check_publish(sealed, data_enabled=True))
    sealed["steps"].insert(1, {"key": "tor", "type": "gate", "title": "Gate"})
    sealed["steps"][2]["title"] = "{{steps.lauf.task_nr}}"
    assert not any(issue.code == "zone_boundary_without_gate" for issue in check_publish(sealed, data_enabled=True))

    broken = {"steps": [{"key": "a", "type": "notify", "title": "A",
                         "outputs": {"extra": {"type": "json", "schema": {"type": "object", "minimum": 1}}}}]}
    assert any(issue.code == "invalid_contract" for issue in check_publish(broken))


def test_conditions_and_parallel_group_publish_checks():
    agent = {"key": "a", "type": "agent", "role": "writer", "title": "A"}
    notify = {"key": "n", "type": "notify", "title": "N"}
    approval = {"key": "ok", "type": "approval", "reviewer": "human:owner", "title": "OK"}

    # `when:` on an agent step is fine and its placeholder is walked.
    ok = {"params": [{"name": "mode", "type": "string"}],
          "steps": [agent | {"condition_tpl": "{{params.mode}}"}]}
    assert blocking(check_publish(ok)) == []
    bad = {"steps": [agent | {"condition_tpl": "{{steps.missing.task_nr}}"}]}
    assert any(issue.code == "unknown_placeholder" for issue in check_publish(bad))

    # `when:` on a decision or zone-boundary step is refused.
    gate = {"key": "tor", "type": "gate", "title": "Gate"}
    assert any(issue.code == "workflow_condition_invalid" for issue in check_publish(
        {"params": [{"name": "mode", "type": "string"}],
         "steps": [approval | {"condition_tpl": "{{params.mode}}"}]}))
    assert any(issue.code == "workflow_condition_invalid" for issue in check_publish(
        {"params": [{"name": "mode", "type": "string"}],
         "steps": [gate | {"condition_tpl": "{{params.mode}}"}]}))

    # A parallel group of two agent/notify steps is fine.
    parallel = {"steps": [agent | {"parallel_group": "drafts"},
                          notify | {"parallel_group": "drafts"}]}
    assert blocking(check_publish(parallel)) == []

    # A lone group name, and a group containing a decision step, are refused.
    assert any(issue.code == "workflow_parallel_invalid" for issue in check_publish(
        {"steps": [agent | {"parallel_group": "solo"}]}))
    assert any(issue.code == "workflow_parallel_invalid" for issue in check_publish(
        {"steps": [agent | {"parallel_group": "g"},
                   approval | {"parallel_group": "g"}]}))


def test_output_contract_rules():
    contract = parse_output_contract({
        "notiz": {"type": "text", "required": False, "max_length": 5},
        "menge": {"type": "decimal", "required": True},
        "aktiv": {"type": "bool", "required": True},
        "monat": {"type": "month", "required": True},
    })
    cleaned = validate_outputs(contract, {"menge": "12.50", "aktiv": "true", "monat": "2026-03"})
    assert cleaned == {"menge": "12.50", "aktiv": True, "monat": "2026-03"}
    with pytest.raises(ContractError):
        validate_outputs(contract, {"menge": "12.50", "aktiv": "true", "monat": "2026-03", "notiz": "zu lang"})
    with pytest.raises(ContractError):
        validate_outputs(contract, {"menge": "x", "aktiv": True, "monat": "2026-03"})
    huge = {"blob": {"type": "text", "required": True}}
    with pytest.raises(ContractError) as too_big:
        validate_outputs(parse_output_contract(huge), {"blob": "x" * (64 * 1024 + 1)})
    assert "KiB" in str(too_big.value)

    files = parse_output_contract({"bericht": {"type": "file", "path_glob": "reports/*.md"}})
    assert validate_outputs(files, {"bericht": "reports/a.md"}, commit="a" * 40,
                            file_exists=lambda path, _commit: True)["bericht"]["path"] == "reports/a.md"
    with pytest.raises(ContractError):
        validate_outputs(files, {"bericht": "reports/sub/a.md"}, commit="a" * 40, file_exists=lambda *_: True)
    with pytest.raises(ContractError):
        validate_outputs(files, {"bericht": "../secret.md"}, commit="a" * 40, file_exists=lambda *_: True)

    commits = parse_output_contract({"stand": {"type": "commit", "required": True}})
    assert validate_outputs(commits, {"stand": "b" * 40}, commit_on_branch=lambda sha: sha == "b" * 40) == {"stand": "b" * 40}
    with pytest.raises(ContractError):
        validate_outputs(commits, {"stand": "b" * 40}, commit_on_branch=lambda _sha: False)


def test_input_bindings_resolve_to_references():
    ctx = Context(
        params={"datensatz": "ds-1"},
        steps={
            "skript": StepValue(step_type="script", status="done", output={
                "script": {"path": "analysis/monatsauswertung.py", "commit": "a" * 40}}),
            "auswertung": StepValue(step_type="runner", status="done", output={"result": "result-9"}),
        })
    bindings = resolve_bindings([
        {"ref": "{{params.datensatz}}", "purpose": "input"},
        {"ref": "{{steps.skript.output.script}}", "purpose": "script"},
        {"ref": "{{steps.auswertung.output.result}}", "purpose": "output"},
    ], ctx)
    assert bindings[0]["value"] == "ds-1"
    assert bindings[1]["path"] == "analysis/monatsauswertung.py" and bindings[1]["commit"] == "a" * 40
    assert bindings[2]["value"] == "result-9"
    with pytest.raises(PlaceholderError):
        resolve_bindings([{"ref": "siehe {{params.datensatz}}", "purpose": "input"}], ctx)


def test_agent_interfaces_carry_outputs(tmp_path):
    body = tmp_path / "kern.md"
    body.write_text(KERN + "\n")
    blob = tmp_path / "outputs.json"
    blob.write_text(json.dumps({"quellen": ["https://example.org/a"]}))
    parsed = parse_output_args([f"kernaussagen=@{body}", "anzahl=12"], blob)
    assert parsed["kernaussagen"] == KERN
    assert parsed["anzahl"] == "12"
    assert parsed["quellen"] == ["https://example.org/a"]

    summary, points, outputs = split_submit_sections(
        "Fertig.\n\n## Outputs\n\n```json\n{\"quellen\": [\"https://example.org/a\"]}\n```\n\n"
        "## Open points\n- Quellen prüfen\n")
    assert summary == "Fertig." and points == ["Quellen prüfen"]
    assert outputs == {"quellen": ["https://example.org/a"]}
    summary, points, outputs = split_submit_sections("Fertig.\n## Open points\n- a\n## Outputs\n{\"datei\": \"content/workflows.md\"}\n")
    assert points == ["a"] and outputs == {"datei": "content/workflows.md"} and "Open points" not in summary
    with pytest.raises(ValueError):
        split_submit_sections("x\n## Outputs\nnot json\n")

    task = {"task_nr": "DEMO-0201", "title": "Recherche", "status": "in_progress", "prio": 1,
            "description": "Suchen", "acceptance_criteria": "Belegt",
            "project_info": {"default_branch": "main", "brain_path": "brain.md"},
            "required_outputs": [
                {"name": "kernaussagen", "type": "text", "required": True, "description": "Kern", "max_length": 4000},
                {"name": "quellen", "type": "json", "required": True},
            ]}
    text = task_markdown(task, footer=False)
    assert "## Required outputs" in text
    assert "`kernaussagen` (text, required)" in text and "max 4000 characters" in text
    assert "tbr submit DEMO-0201" in text and "--output-json outputs.json" in text
    assert "\n## Outputs\n" not in text
    task["workflow_outputs"] = {"task_nr": "DEMO-0201", "output": {"kernaussagen": KERN}}
    assert "\n## Outputs\n" in task_markdown(task, footer=False)
    assert required_outputs_lines({"required_outputs": []}) == []
