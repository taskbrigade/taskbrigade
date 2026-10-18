"""Output gate checks 1–9 without a database (research/konzept-datenzonen.md §6): `app/services/gate_checks.py` and the
sandboxed probe for checks 4 and 5 (`app/runner/gate_probe.py`), each with bypass attempts."""
import json
import os
import subprocess
import sys

import pytest

from app.runner import gate_probe
from app.services import gate_checks as gc

OK_PROBE = {"format": gc.PROBE_FORMAT, "error": None, "rows": 40, "columns": 3, "identifier_columns": 1,
            "identifier_hits": [], "identifier_hit_count": 0, "raw_row_hits": [], "raw_row_hit_count": 0}


def table(rows, columns=("month", "n", "total"), name="per_month", count="n", **extra):
    out = {"name": name, "columns": list(columns), "rows": [list(r) for r in rows], **extra}
    if count is not None:
        out["count_column"] = count
    return out


def doc(*tables, metrics=None, **extra):
    out = {"format": gc.FORMAT, "tables": list(tables) or [table([["2026-01", 12, 100.5]])], **extra}
    if metrics is not None:
        out["metrics"] = metrics
    return out


def ev(d, probe=OK_PROBE, cfg=None, outputs=("result.json",), released=None, refused=0, raw=None):
    data = raw if raw is not None else json.dumps(d).encode()
    return gc.evaluate(data, list(outputs), probe, cfg or gc.Config(), released, refused)


def status(report, n):
    return next(c for c in report.checks if c["n"] == n)


def failed(report):
    return [(c["n"], c.get("code")) for c in report.checks if c["status"] == "failed"]


def test_valid_aggregate_passes_every_check():
    r = ev(doc(metrics={"rows_total": 1830}))
    assert r.failed == [] and r.warnings == []
    assert [c["status"] for c in r.checks] == ["ok"] * 6 + ["skipped", "ok", "applied"]
    assert len(r.checks) == 9 and json.loads(r.as_json(5))["k"] == 5


# --- Check 1: format (incl. raw rows in disguise) ----------------------------------------------------------------

@pytest.mark.parametrize("bad, code", [
    ({"format": "csv", "tables": []}, "wrong_format"),
    (doc(raw=[["Max Mustermann", "DE89370400440532013000"]]), "unknown_keys"),  # Rows smuggled in an extra key
    (doc(table([["2026-01", 12, {"name": "Max"}]])), "bad_row"),  # Nested object in a cell
    (doc(table([["2026-01", 12, ["a", "b"]]])), "bad_row"),  # Nested list
    (doc(table([["2026-01", 12]])), "bad_row"),  # Row shorter than the columns
    (doc(table([["2026-01", 12, 1.0]], name="t"), table([["2026-02", 9, 2.0]], name="t")), "bad_table_name"),
    (doc(table([["x", 12, 1]], extra_rows=[["Max"]])), "bad_table"),  # Unknown table key
    (doc(table([["x", 12, 1]], columns=("a", "a", "b"))), "bad_columns"),
    (doc(table([["x", 12, 1]], count="count")), "bad_count_column"),
    (doc(metrics={"customer": "Max Mustermann"}), "bad_metrics"),  # Raw value as a metric
    (doc(metrics={"x": [1, 2]}), "bad_metrics"),
    (doc(table([["x", 12, 1, 1]], columns=("m", "n", "total", "top"), max_contribution={"total": "n"})),
     "bad_max_contribution"),  # The count column cannot be a helper column (would be dropped)
])
def test_format_violations_are_hard(bad, code):
    r = ev(bad)
    assert failed(r) == [(1, code)]
    assert [c["status"] for c in r.checks[1:]] == ["not_run"] * 8  # The first hard failure stops


def test_format_refuses_non_json_nan_and_extra_files():
    assert failed(ev(None, raw=b"month;n\n2026-01;3\n")) == [(1, "not_json")]
    assert failed(ev(None, raw=b'{"format": "tbr-aggregate/1", "tables": [{"name": "t", "columns": ["n"], '
                                b'"count_column": "n", "rows": [[NaN]]}]}')) == [(1, "bad_row")]
    assert failed(ev(doc(), outputs=("result.json", "rows.csv"))) == [(1, "extra_files")]
    assert failed(ev(doc(), refused=1)) == [(1, "extra_files")]  # A symlink or bad name left in out/
    assert failed(ev(doc(), outputs=())) == [(1, "missing")]
    assert failed(ev(None, raw=b"[" * 100000 + b"]" * 100000)) == [(1, "not_json")]  # Deep nesting
    assert failed(gc.failed_report("unreadable")) == [(1, "unreadable")]


# --- Check 2: size -----------------------------------------------------------------------------------------------

def test_size_limits():
    many_rows = doc(table([[f"g{i}", 10, 1.0] for i in range(1001)]))
    assert failed(ev(many_rows)) == [(2, "too_many_rows")]
    assert ev(many_rows, cfg=gc.Config(max_rows=2000)).failed == []  # gate_max_rows
    many_tables = doc(*[table([["x", 10, 1]], name=f"t{i}") for i in range(21)])
    assert failed(ev(many_tables)) == [(2, "too_many_tables")]
    padded = json.dumps(doc()).encode()[:-1] + b" " * gc.MAX_BYTES + b"}"
    assert failed(ev(None, raw=padded)) == [(2, "too_large")]


# --- Check 3: group size (small groups and their disguises) ------------------------------------------------------

@pytest.mark.parametrize("n, code", [(4, "small_group"), (1, "small_group"), (0, "small_group"),
                                     (-3, "small_group"), (4.9, "count_not_integer"), (5.0, "count_not_integer"),
                                     ("5", "count_not_integer"), (True, "count_not_integer"),
                                     (None, "count_not_integer")])
def test_small_groups_fail(n, code):
    r = ev(doc(table([["2026-01", 12, 1.0], ["2026-02", n, 2.0]])))
    entry = status(r, 3)
    assert (entry["status"], entry["code"]) == ("failed", code)
    assert entry["where"] == ["t0.r1.c1"]  # A location, never the value


def test_group_size_needs_a_count_column_in_every_table():
    r = ev(doc(table([["2026-01", 12, 1.0]]), table([["Max", 99]], columns=("name", "x"), name="raw", count=None)))
    assert failed(r) == [(3, "no_count_column")] and status(r, 3)["where"] == ["t1"]


def test_k_is_configurable():
    d = doc(table([["2026-01", 3, 1.0]]))
    assert failed(ev(d)) == [(3, "small_group")]
    assert ev(d, cfg=gc.Config(k=3)).failed == []
    assert status(ev(d, cfg=gc.Config(k=3)), 3)["params"] == {"k": 3}


# --- Checks 4 and 5: from the probe, fail closed -----------------------------------------------------------------

def test_probe_results_fail_closed():
    assert failed(ev(doc(), probe=None)) == [(4, "probe_missing")]
    assert failed(ev(doc(), probe={"format": "other"})) == [(4, "probe_missing")]
    assert failed(ev(doc(), probe={"format": gc.PROBE_FORMAT, "error": "xlsx_unsupported"})) == [(4, "probe_error")]
    hit = {**OK_PROBE, "identifier_hits": ["t0.r0.c0"], "identifier_hit_count": 1}
    r = ev(doc(), probe=hit)
    assert failed(r) == [(4, "identifier_values")] and status(r, 4)["where"] == ["t0.r0.c0"]
    raw = {**OK_PROBE, "raw_row_hits": ["t0.r0"], "raw_row_hit_count": 3}
    r = ev(doc(), probe=raw)
    assert failed(r) == [(5, "raw_rows")] and status(r, 5)["params"] == {"hits": 3}
    assert ev(doc(), probe={**OK_PROBE, "raw_row_hit_count": 1}).failed == [5]  # Count without locations


# --- Warnings 6–8 and transformation 9 ---------------------------------------------------------------------------

def test_free_text_warns_on_long_strings_and_digit_runs():
    d = doc(table([["x" * 65, 10, 1.0], ["DE89 3704 0044 0532 0130 00", 10, 1.0], ["2026-01", 10, 1.0]]),
            metrics={"phone 0711 1234567": 1})
    r = ev(d)
    entry = status(r, 6)
    assert r.failed == [] and entry["status"] == "warning"
    assert entry["where"] == ["t0.r0.c0", "t0.r1.c0", "m0"]


def test_dominance_warns_or_fails_and_drops_the_helper_column():
    d = doc(table([["2026-01", 10, 100.0, 91.0], ["2026-02", 10, 100.0, 50.0]], columns=("month", "n", "total", "top"),
                  max_contribution={"total": "top"}))
    r = ev(d)
    assert status(r, 7)["status"] == "warning" and status(r, 7)["where"] == ["t0.r0.c2"]
    hard = ev(d, cfg=gc.Config(dominance_hard=True))
    assert failed(hard) == [(7, "dominant")]
    assert ev(d, cfg=gc.Config(dominance_pct=95)).warnings == []
    released = gc.release_content(r.doc)
    assert released["tables"][0]["columns"] == ["month", "n", "total"]  # The largest single value stays in zone R
    assert all(len(row) == 3 for row in released["tables"][0]["rows"])
    assert status(ev(doc()), 7)["status"] == "skipped"  # Nothing declared: not checkable


def test_differencing_against_released_results():
    before = doc(table([["2026-01", 20, 1.0], ["2026-02", 30, 2.0]]))
    close = doc(table([["2026-01", 21, 1.0], ["2026-02", 30, 2.0]]))
    r = ev(close, released=[before])
    assert status(r, 8)["status"] == "warning" and status(r, 8)["where"] == ["t0.r0.c1"]
    far = doc(table([["2026-01", 25, 1.0]]))
    assert status(ev(far, released=[before]), 8)["status"] == "ok"
    other_table = doc(table([["2026-01", 21, 1.0]], name="other"))
    assert status(ev(other_table, released=[before]), 8)["status"] == "ok"
    total_before = doc(table([[40]], columns=("n",)))
    assert status(ev(doc(table([[41]], columns=("n",))), released=[total_before]), 8)["status"] == "warning"


def test_precision_rounds_floats_on_release():
    d = doc(table([["2026-01", 12, 1234.5678], ["2026-02", 12, -0.001]]), metrics={"avg": 2.345678, "rows": 24})
    r = ev(d)
    assert status(r, 9) == {"n": 9, "key": "precision", "kind": "transformation", "status": "applied",
                            "params": {"rounded": 3, "decimals": 2}}
    out = gc.release_content(r.doc)
    assert out["tables"][0]["rows"] == [["2026-01", 12, 1234.57], ["2026-02", 12, 0.0]]
    assert out["metrics"] == {"avg": 2.35, "rows": 24}
    assert str(out["tables"][0]["rows"][1][2]) == "0.0"  # No "-0.0"


def test_report_holds_no_values():
    d = doc(table([["Max Mustermann", 2, 1.0]]))
    text = ev(d).as_json(5)
    assert "Mustermann" not in text and "small_group" in text


# --- Probe (checks 4 and 5) --------------------------------------------------------------------------------------

CUSTOMERS = ("customer;contact;account;amount\n"
             "Max Mustermann;max@example.org;4400;12.50\n"
             "Erika Musterfrau;erika@example.org;4400;99.00\n"
             "Hans Meier;hans@example.org;4410;7.25\n"
             "Anna Schmidt;anna@example.org;4410;14.00\n")


def run_probe(tmp_path, result, content=CUSTOMERS, name="data.csv"):
    (tmp_path / "in").mkdir(parents=True, exist_ok=True)
    data = content.encode() if isinstance(content, str) else content
    (tmp_path / "in" / name).write_bytes(data)
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result))
    return gate_probe.probe(str(tmp_path / "in"), str(path))


@pytest.mark.parametrize("cell", ["Max Mustermann", "  MAX   mustermann ", "Kunde: Max Mustermann (VIP)",
                                  "max@example.org", "MAX@EXAMPLE.ORG", "Erika-Musterfrau"])
def test_probe_finds_identifier_values_in_disguise(tmp_path, cell):
    report = run_probe(tmp_path, doc(table([[cell, 12, 1.0]])))
    assert report["error"] is None and report["identifier_hits"] == ["t0.r0.c0"]
    assert report["identifier_columns"] >= 2  # customer (name and cardinality), contact (pattern)


def test_probe_checks_names_and_metric_keys_too(tmp_path):
    result = doc(table([["2026-01", 12, 1.0]], columns=("month", "n", "Hans Meier"), name="Anna Schmidt"),
                 metrics={"erika@example.org": 1})
    report = run_probe(tmp_path, result)
    assert sorted(report["identifier_hits"]) == ["m0", "t0.col2", "t0.name"]


def test_probe_leaves_categories_alone(tmp_path):
    report = run_probe(tmp_path, doc(table([["4400", 2, 111.5], ["4410", 2, 21.25]], columns=("account", "n", "sum"))))
    assert report["identifier_hits"] == [] and report["raw_row_hits"] == [] and report["rows"] == 4


def test_probe_leaves_dates_and_amounts_alone(tmp_path):
    """Dates and amounts are not identifiers, even when every value is distinct: daily aggregates pass."""
    bookings = "booking_date;amount;ref\n" + "".join(f"2026-01-{d:02d};{d}234.56;0711 12345{d:02d}\n" for d in range(1, 9))
    result = doc(table([["2026-01-01", 5, 1.0], ["2026-01-02", 5, 2.0]], columns=("day", "n", "sum")))
    report = run_probe(tmp_path, result, content=bookings)
    assert report["identifier_columns"] == 1 and report["identifier_hits"] == []  # Only the phone-like `ref`
    leak = doc(table([["0711 1234503", 5, 1.0]], columns=("ref", "n", "sum")))
    assert run_probe(tmp_path / "x", leak, content=bookings)["identifier_hits"] == ["t0.r0.c0"]


def test_probe_finds_raw_rows_in_disguise(tmp_path):
    rows = [[12.5, "MAX@example.org", "4400", "max mustermann"],  # Reordered, case changed, 12.50 → 12.5
            ["4400", "Erika Musterfrau", "erika@example.org", 99]]  # 99.00 as an integer
    result = doc(table(rows, columns=("a", "b", "c", "d"), count=None))
    report = run_probe(tmp_path, result)
    assert report["raw_row_hits"] == ["t0.r0", "t0.r1"] and report["raw_row_hit_count"] == 2


def test_probe_catches_a_dropped_column_in_wide_rows(tmp_path):
    wide = "a;b;c;d;e;f\n" + "".join(f"k{i};{i}x;{i}y;{i}z;{i}w;{i}v\n" for i in range(5))
    result = doc(table([["1x", "1y", "1z", "1w", "1v"]], columns=("b", "c", "d", "e", "f"), count=None))
    assert run_probe(tmp_path, result, content=wide)["raw_row_hits"] == ["t0.r0"]


def test_probe_reads_tsv_and_json_and_refuses_unknown_formats(tmp_path):
    tsv = CUSTOMERS.replace(";", "\t")
    assert run_probe(tmp_path / "a", doc(table([["Hans Meier", 9, 1.0]])), tsv, "data.tsv")["identifier_hits"]
    rows = [{"customer": "Hans Meier", "amount": 1}, {"customer": "Anna Schmidt", "amount": 2}]
    assert run_probe(tmp_path / "b", doc(table([["Hans Meier", 9, 1.0]])), json.dumps(rows), "data.json")[
        "identifier_hits"] == ["t0.r0.c0"]
    with pytest.raises(gate_probe.ProbeError, match="input_unsupported"):
        run_probe(tmp_path / "c", doc(), b"\xd0\xcf", "data.xls")


def test_probe_needs_openpyxl_for_excel(tmp_path):
    """The runner venv has openpyxl (deploy/runner-requirements.txt); without it Excel input fails closed."""
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        with pytest.raises(gate_probe.ProbeError, match="xlsx_unsupported"):
            run_probe(tmp_path, doc(), b"PK\x03\x04", "data.xlsx")
    else:
        pytest.skip("openpyxl installed: Excel input is parsed")


def test_probe_main_writes_a_value_free_report(tmp_path):
    """As the sandbox runs it: `python -I entry.py` with result.json next to it."""
    for sub in ("in", "out", "script"):
        (tmp_path / sub).mkdir()
    (tmp_path / "in" / "data.csv").write_text(CUSTOMERS)
    (tmp_path / "script" / "entry.py").write_bytes(open(gate_probe.__file__, "rb").read())
    (tmp_path / "script" / "result.json").write_text(json.dumps(doc(table([["Max Mustermann", 9, 1.0]]))))
    env = {"TBR_INPUT": str(tmp_path / "in"), "TBR_OUTPUT_DIR": str(tmp_path / "out"), "PATH": os.environ["PATH"]}
    subprocess.run([sys.executable, "-I", "-B", str(tmp_path / "script" / "entry.py")], env=env, check=True,
                   cwd=tmp_path / "script")
    text = (tmp_path / "out" / "probe.json").read_text()
    assert json.loads(text)["identifier_hits"] == ["t0.r0.c0"]
    assert "Mustermann" not in text and "example.org" not in text
    (tmp_path / "script" / "result.json").write_text("not json")
    os.remove(tmp_path / "out" / "probe.json")
    subprocess.run([sys.executable, "-I", "-B", str(tmp_path / "script" / "entry.py")], env=env, check=True,
                   cwd=tmp_path / "script")
    assert json.loads((tmp_path / "out" / "probe.json").read_text())["error"] == "result_unreadable"
