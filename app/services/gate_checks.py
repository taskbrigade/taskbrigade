"""Automatic checks 1–9 of the output gate (research/konzept-datenzonen.md §6, guide docs/data-store.md → Output gate).

Pure functions without database access. `evaluate` takes the plaintext `result.json` of a runner job, the names
of the job's output files, the report of the sandboxed probe (checks 4 and 5, `app/runner/gate_probe.py`), the
settings and the contents of results released earlier for the same dataset (check 8). The report holds codes,
counts and locations (`t0.r3.c1` = table 0, row 3, column 1) – never a value of the output or the input, so it can
be stored in `run_results.checks_json` and shown to anyone allowed to see the release view.

Order as in the concept: the first failed hard check stops; the checks after it are recorded as `not_run`.
Warnings go to the person who decides. Check 9 rounds floats (a transformation, not only a check); `release_content`
applies it and drops the helper columns a table declares for check 7.
"""
import json
import math
import re
from dataclasses import dataclass, field

FORMAT = "tbr-aggregate/1"
PROBE_FORMAT = "tbr-gate-probe/1"
RESULT_FILE = "result.json"
MAX_BYTES = 1024 * 1024
PARSE_LIMIT = 16 * MAX_BYTES  # Larger files are not even decrypted or parsed (check 1 fails)
MAX_TABLES = 20
MAX_NAME = 100
MAX_WHERE = 50
FREE_TEXT_CHARS = 64
DIGITS = re.compile(r"\d(?:[\s./-]?\d){7,}")  # Eight or more digits, also with separators (IBAN, phone)
DECIMALS = 2
TABLE_KEYS = {"name", "columns", "count_column", "rows", "max_contribution"}
TOP_KEYS = {"format", "tables", "metrics"}
CHECKS = ((1, "format", "hard"), (2, "size", "hard"), (3, "group_size", "hard"), (4, "identifiers", "hard"),
          (5, "raw_rows", "hard"), (6, "free_text", "warning"), (7, "dominance", "warning"),
          (8, "differencing", "warning"), (9, "precision", "transformation"))


@dataclass
class Config:
    k: int = 5
    max_rows: int = 1000
    dominance_pct: int = 85
    dominance_hard: bool = False
    max_bytes: int = MAX_BYTES
    max_tables: int = MAX_TABLES


@dataclass
class Report:
    checks: list[dict] = field(default_factory=list)
    doc: dict | None = None

    @property
    def failed(self) -> list[int]:
        return [c["n"] for c in self.checks if c["status"] == "failed"]

    @property
    def warnings(self) -> list[int]:
        return [c["n"] for c in self.checks if c["status"] == "warning"]

    def as_json(self, k: int) -> str:
        return json.dumps({"version": 1, "k": k, "failed": self.failed, "warnings": self.warnings,
                           "checks": self.checks}, sort_keys=True)


class Failure(Exception):
    """A hard check failed: `code` names the rule, `where` the locations, `params` counts and limits."""

    def __init__(self, code: str, where: list[str] | None = None, **params):
        super().__init__(code)
        self.code, self.where, self.params = code, where or [], params


def _entry(n: int, status: str, code: str | None = None, where: list[str] | None = None, **params) -> dict:
    key, kind = next((k, kind) for i, k, kind in CHECKS if i == n)
    out = {"n": n, "key": key, "kind": kind, "status": status}
    if code:
        out["code"] = code
    if where:
        out["where"] = where[:MAX_WHERE]
        out["count"] = len(where)
    if params:
        out["params"] = params
    return out


def _scalar(value) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    return value is None or isinstance(value, (str, int, bool))


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# --- Check 1: format ---------------------------------------------------------------------------------------------

def check_format(data: bytes, outputs: list[str], refused: int = 0) -> dict:
    extra = sorted(n for n in outputs if n != RESULT_FILE)
    if extra or refused:
        raise Failure("extra_files", count=len(extra) + refused)
    if RESULT_FILE not in outputs:
        raise Failure("missing")
    if len(data) > PARSE_LIMIT:
        raise Failure("too_large_to_parse", limit=PARSE_LIMIT)
    try:
        doc = json.loads(data.decode("utf-8"), parse_constant=lambda _c: float("nan"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise Failure("not_json") from e
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise Failure("wrong_format")
    if set(doc) - TOP_KEYS:
        raise Failure("unknown_keys")
    tables = doc.get("tables")
    if not isinstance(tables, list):
        raise Failure("no_tables")
    names = set()
    for t, table in enumerate(tables):
        where = [f"t{t}"]
        if not isinstance(table, dict) or set(table) - TABLE_KEYS:
            raise Failure("bad_table", where)
        name, columns, rows = table.get("name"), table.get("columns"), table.get("rows")
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME or name in names:
            raise Failure("bad_table_name", where)
        names.add(name)
        if (not isinstance(columns, list) or not columns or len(set(map(str, columns))) != len(columns)
                or not all(isinstance(c, str) and c.strip() and len(c) <= MAX_NAME for c in columns)):
            raise Failure("bad_columns", where)
        if not isinstance(rows, list):
            raise Failure("bad_rows", where)
        for r, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != len(columns) or not all(_scalar(c) for c in row):
                raise Failure("bad_row", [f"t{t}.r{r}"])
        count = table.get("count_column")
        if count is not None and count not in columns:
            raise Failure("bad_count_column", where)
        extra_cols = table.get("max_contribution")
        if extra_cols is not None:
            if (not isinstance(extra_cols, dict) or not extra_cols
                    or any(s not in columns or m not in columns or s == m or count in (s, m)
                           for s, m in extra_cols.items())
                    or len(set(extra_cols.values())) != len(extra_cols)
                    or set(extra_cols) & set(extra_cols.values())):
                raise Failure("bad_max_contribution", where)
    metrics = doc.get("metrics", {})
    if not isinstance(metrics, dict) or not all(isinstance(k, str) and len(k) <= MAX_NAME and _number(v)
                                                for k, v in metrics.items()):
        raise Failure("bad_metrics")
    return doc


# --- Check 2: size -----------------------------------------------------------------------------------------------

def check_size(data: bytes, doc: dict, cfg: Config) -> dict:
    tables = doc["tables"]
    rows = sum(len(t["rows"]) for t in tables)
    params = {"bytes": len(data), "tables": len(tables), "rows": rows}
    if len(data) > cfg.max_bytes:
        raise Failure("too_large", limit=cfg.max_bytes, **params)
    if len(tables) > cfg.max_tables:
        raise Failure("too_many_tables", limit=cfg.max_tables, **params)
    big = [f"t{t}" for t, table in enumerate(tables) if len(table["rows"]) > cfg.max_rows]
    if big:
        raise Failure("too_many_rows", big, limit=cfg.max_rows, **params)
    return params


# --- Check 3: group size -----------------------------------------------------------------------------------------

def check_group_size(doc: dict, cfg: Config) -> dict:
    missing = [f"t{t}" for t, table in enumerate(doc["tables"]) if not table.get("count_column")]
    if missing:
        raise Failure("no_count_column", missing)
    not_int, small = [], []
    for t, table in enumerate(doc["tables"]):
        c = table["columns"].index(table["count_column"])
        for r, row in enumerate(table["rows"]):
            n = row[c]
            if not isinstance(n, int) or isinstance(n, bool):
                not_int.append(f"t{t}.r{r}.c{c}")
            elif n < cfg.k:
                small.append(f"t{t}.r{r}.c{c}")
    if not_int:
        raise Failure("count_not_integer", not_int)
    if small:
        raise Failure("small_group", small, k=cfg.k)
    return {"k": cfg.k}


# --- Checks 4 and 5: from the probe ------------------------------------------------------------------------------

def check_probe(probe: dict | None, n: int) -> dict:
    """Fail closed: no probe report, a report in another format or a probe error fails the check."""
    if not isinstance(probe, dict) or probe.get("format") != PROBE_FORMAT:
        raise Failure("probe_missing")
    if probe.get("error"):
        raise Failure("probe_error", reason=str(probe["error"])[:40])
    hits, count = (("identifier_hits", "identifier_hit_count") if n == 4 else ("raw_row_hits", "raw_row_hit_count"))
    where = [str(h)[:40] for h in probe.get(hits) or []]
    total = int(probe.get(count) or len(where))
    if where or total:
        raise Failure("identifier_values" if n == 4 else "raw_rows", where, hits=total)
    params = {"input_rows": int(probe.get("rows") or 0)}
    if n == 4:
        params["identifier_columns"] = int(probe.get("identifier_columns") or 0)
    return params


# --- Check 6: free text ------------------------------------------------------------------------------------------

def strings(doc: dict):
    for t, table in enumerate(doc["tables"]):
        yield f"t{t}.name", table["name"]
        for c, col in enumerate(table["columns"]):
            yield f"t{t}.col{c}", col
        for r, row in enumerate(table["rows"]):
            for c, cell in enumerate(row):
                if isinstance(cell, str):
                    yield f"t{t}.r{r}.c{c}", cell
    for m, key in enumerate(doc.get("metrics") or {}):
        yield f"m{m}", key


def check_free_text(doc: dict) -> tuple[str, list[str]]:
    long_, digits = [], []
    for where, text in strings(doc):
        if len(text) > FREE_TEXT_CHARS:
            long_.append(where)
        elif DIGITS.search(text):
            digits.append(where)
    return ("free_text", long_ + digits) if long_ or digits else ("", [])


# --- Check 7: dominance ------------------------------------------------------------------------------------------

def check_dominance(doc: dict, cfg: Config) -> tuple[str, list[str]]:
    declared, where = False, []
    for t, table in enumerate(doc["tables"]):
        pairs = table.get("max_contribution") or {}
        declared = declared or bool(pairs)
        for sum_col, max_col in pairs.items():
            s, m = table["columns"].index(sum_col), table["columns"].index(max_col)
            for r, row in enumerate(table["rows"]):
                total, top = row[s], row[m]
                if not (_number(total) and _number(top)):
                    where.append(f"t{t}.r{r}.c{m}")
                elif total and abs(top) * 100 > abs(total) * cfg.dominance_pct:
                    where.append(f"t{t}.r{r}.c{s}")
    if not declared:
        return "not_declared", []
    return ("dominant", where) if where else ("", [])


# --- Check 8: differencing ---------------------------------------------------------------------------------------

def _key_columns(table: dict) -> list[int]:
    skip = {table.get("count_column")} | set((table.get("max_contribution") or {}).values())
    return [i for i, col in enumerate(table["columns"])
            if col not in skip and all(isinstance(row[i], str) or row[i] is None for row in table["rows"])]


def _groups(table: dict) -> dict[tuple, int]:
    cols = table["columns"]
    if table.get("count_column") not in cols:
        return {}
    c = cols.index(table["count_column"])
    keys = _key_columns(table)
    out = {}
    for row in table["rows"]:
        if not isinstance(row[c], int):
            continue
        if keys:
            out[tuple((cols[i], row[i]) for i in keys)] = row[c]
        elif len(table["rows"]) == 1:
            out[()] = row[c]
    return out


def check_differencing(doc: dict, released: list[dict], cfg: Config) -> list[str]:
    where = []
    for t, table in enumerate(doc["tables"]):
        new = _groups(table)
        cols = table["columns"]
        c = cols.index(table["count_column"])
        for old_doc in released:
            for old in old_doc.get("tables") or []:
                if not isinstance(old, dict) or old.get("name") != table["name"]:
                    continue
                before = _groups(old)
                for r, row in enumerate(table["rows"]):
                    keys = _key_columns(table)
                    key = tuple((cols[i], row[i]) for i in keys) if keys else (() if len(table["rows"]) == 1
                                                                               else None)
                    if key is None or key not in before or key not in new:
                        continue
                    diff = abs(new[key] - before[key])
                    if 0 < diff < cfg.k:
                        where.append(f"t{t}.r{r}.c{c}")
    return sorted(set(where))


# --- Check 9: precision and the released form -------------------------------------------------------------------

def _round(value):
    if isinstance(value, float):
        rounded = round(value, DECIMALS)
        return rounded + 0.0 if rounded else 0.0  # No "-0.0"
    return value


def rounded_count(doc: dict) -> int:
    n = sum(1 for t in doc["tables"] for row in t["rows"] for v in row if isinstance(v, float) and _round(v) != v)
    return n + sum(1 for v in (doc.get("metrics") or {}).values() if isinstance(v, float) and _round(v) != v)


def release_content(doc: dict) -> dict:
    """What leaves zone R: floats rounded to two decimals, helper columns of check 7 removed."""
    tables = []
    for table in doc["tables"]:
        drop = set((table.get("max_contribution") or {}).values())
        keep = [i for i, col in enumerate(table["columns"]) if col not in drop]
        out = {"name": table["name"], "columns": [table["columns"][i] for i in keep],
               "count_column": table["count_column"], "rows": [[_round(row[i]) for i in keep] for row in table["rows"]]}
        tables.append(out)
    return {"format": FORMAT, "tables": tables,
            "metrics": {k: _round(v) for k, v in (doc.get("metrics") or {}).items()}}


def failed_report(code: str) -> Report:
    """Check 1 failed before anything could be parsed (e.g. the quarantined file cannot be decrypted)."""
    report = Report()
    report.checks = [_entry(1, "failed", code)] + [_entry(i, "not_run") for i, _key, _kind in CHECKS if i > 1]
    return report


# --- All checks --------------------------------------------------------------------------------------------------

def evaluate(data: bytes | None, outputs: list[str], probe: dict | None, cfg: Config,
             released: list[dict] | None = None, refused: int = 0) -> Report:
    report = Report()
    steps = [
        (1, lambda: check_format(data or b"", outputs, refused)),
        (2, lambda: check_size(data, report.doc, cfg)),
        (3, lambda: check_group_size(report.doc, cfg)),
        (4, lambda: check_probe(probe, 4)),
        (5, lambda: check_probe(probe, 5)),
    ]
    for n, step in steps:
        try:
            value = step()
        except Failure as e:
            report.checks.append(_entry(n, "failed", e.code, e.where, **e.params))
            report.checks += [_entry(i, "not_run") for i, _key, _kind in CHECKS if i > n]
            return report
        if n == 1:
            report.doc = value
            report.checks.append(_entry(1, "ok", tables=len(value["tables"])))
        else:
            report.checks.append(_entry(n, "ok", **value))
    doc = report.doc
    code, where = check_free_text(doc)
    report.checks.append(_entry(6, "warning" if where else "ok", code or None, where))
    code, where = check_dominance(doc, cfg)
    if code == "not_declared":
        report.checks.append(_entry(7, "skipped", code))
    else:
        status = ("failed" if cfg.dominance_hard else "warning") if where else "ok"
        report.checks.append(_entry(7, status, code or None, where, pct=cfg.dominance_pct))
        if status == "failed":
            report.checks += [_entry(i, "not_run") for i in (8, 9)]
            return report
    where = check_differencing(doc, released or [], cfg)
    report.checks.append(_entry(8, "warning" if where else "ok", "close_to_released" if where else None, where,
                                k=cfg.k, compared=len(released or [])))
    report.checks.append(_entry(9, "applied", rounded=rounded_count(doc), decimals=DECIMALS))
    return report
