"""Output gate probe: checks 4 and 5 of research/konzept-datenzonen.md §6 (guide docs/data-store.md → Output gate).

Runs as its own sandbox job (`taskbrigade-job@<job>-gate`) right after the script's job, never inside it: the runner
copies this file as `script/entry.py`, the script's `result.json` as `script/result.json` and moves the decrypted
input into the new job's `in/`. The input is untrusted (spreadsheet parsers), so it is parsed here, without network
and without access to the key or the store – not in the runner process that holds the KEK.

Self-contained on purpose: the sandbox runs `python -I` from the runner virtualenv and cannot import `app`. Only the
standard library, plus `openpyxl`/`pyarrow` when the input needs them (both are in deploy/runner-requirements.txt).

Writes `$TBR_OUTPUT_DIR/probe.json` with counts and locations only (`t0.r3.c1` = table 0, row 3, column 1) – never a
value from the input or the output:

- check 4 (identifiers): columns of the input that look like identifiers or personal data (column name, value
  pattern, or a text column whose distinct values exceed half the rows); any output string cell, column name, table
  name or metric key that equals one of their values (case, spacing and separators normalised; also any run of up to
  four words inside a longer string) is a hit.
- check 5 (raw rows): an output row that reproduces a whole input row (cells compared as a set, so reordered columns
  match too; for rows of six or more cells one missing cell still counts).
"""
import csv
import io
import json
import os
import re
import sys
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

FORMAT = "tbr-gate-probe/1"
MAX_HITS = 100
MIN_IDENTIFIER_CHARS = 3
WINDOW_WORDS = 4
TEXT_SUFFIXES = (".csv", ".tsv", ".txt")
NAME_HINT = re.compile(r"name|kunde|customer|client|mandant|patient|person|employee|mitarbeiter|e-?mail|iban|"
                       r"telefon|phone|mobil|steuer-?id|tax.?id|ssn|versicherten|geburt|birth|adresse|address|"
                       r"stra(ss|ß)e|street", re.I)
# Checked on the cell text as written (case folded, before numbers are canonicalised), never on dates or amounts
PATTERNS = (
    re.compile(r"^[a-z]{2}\d{2}(?: ?[a-z0-9]{4}){2,7}(?: ?[a-z0-9]{1,4})?$"),  # IBAN, with or without spaces
    re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$"),  # Email
    re.compile(r"^(?:\+|00|0)\d[\d /()-]{5,18}\d$"),  # Phone: international or with a leading 0
    re.compile(r"^\d{11}$"),  # German tax id
)
NUMBER = re.compile(r"^[-+]?\d+(\.\d+)?$")
WORD_SPLIT = re.compile(r"[^\w@.+-]+")


class ProbeError(Exception):
    pass


# --- Normalisation (both sides use the same rules) ---------------------------------------------------------------

def norm(value) -> str | None:
    """Comparable text of a cell: case folded, whitespace collapsed, numbers in canonical form (42.50 → 42.5),
    dates as ISO. None for empty cells."""
    if value is None:
        return None
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        text = format(Decimal(repr(value)).normalize(), "f")
    elif isinstance(value, datetime):
        text = value.date().isoformat() if value.time() == time() else value.isoformat(sep=" ")
    elif isinstance(value, date):
        text = value.isoformat()
    else:
        text = str(value)
    text = " ".join(text.split()).casefold()
    if NUMBER.match(text):
        try:
            text = format(Decimal(text).normalize(), "f")
        except InvalidOperation:
            pass
        if text == "-0":
            text = "0"
    return text or None


def compact(text: str) -> str:
    return re.sub(r"[\s\-/.]+", "", text)


def variants(text: str) -> set[str]:
    words = [w for w in WORD_SPLIT.split(text) if w]
    out = {text, compact(text)}
    if words:
        out.add(" ".join(words))
    return {v for v in out if len(v) >= MIN_IDENTIFIER_CHARS}


def windows(text: str) -> set[str]:
    """`text` itself plus every run of up to four words inside it, each also without separators."""
    out = set(variants(text))
    words = [w for w in WORD_SPLIT.split(text) if w]
    for size in range(1, WINDOW_WORDS + 1):
        for i in range(len(words) - size + 1):
            part = " ".join(words[i:i + size])
            out.add(part)
            out.add(compact(part))
    return {v for v in out if len(v) >= MIN_IDENTIFIER_CHARS}


# --- Input -------------------------------------------------------------------------------------------------------

def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _text_sheets(path: str, suffix: str):
    with open(path, "rb") as f:
        text = _decode(f.read())
    if suffix == ".tsv":
        delimiter = "\t"
    else:
        try:
            delimiter = csv.Sniffer().sniff(text[:65536], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ";" if text[:4096].count(";") > text[:4096].count(",") else ","
    yield csv.reader(io.StringIO(text), delimiter=delimiter)


def _xlsx_sheets(path: str):
    try:
        import openpyxl
    except ImportError as e:
        raise ProbeError("xlsx_unsupported") from e
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            yield ws.iter_rows(values_only=True)
    finally:
        wb.close()


def _json_sheets(path: str):
    with open(path, "rb") as f:
        data = json.loads(_decode(f.read()))
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    if not isinstance(data, list):
        raise ProbeError("json_unsupported")
    if data and all(isinstance(r, dict) for r in data):
        header = list(dict.fromkeys(k for r in data for k in r))
        yield [header] + [[r.get(k) for k in header] for r in data]
    else:
        yield [r if isinstance(r, list) else [r] for r in data]


def _parquet_sheets(path: str):
    try:
        import pyarrow.parquet as pq
    except ImportError as e:
        raise ProbeError("parquet_unsupported") from e
    table = pq.read_table(path)
    yield [table.column_names] + [list(r.values()) for r in table.to_pylist()]


def sheets(path: str):
    """Rows of every sheet (first row = header) of the input file."""
    suffix = os.path.splitext(path)[1].lower()
    if suffix in TEXT_SUFFIXES:
        return _text_sheets(path, suffix)
    if suffix in (".xlsx", ".xlsm"):
        return _xlsx_sheets(path)
    if suffix == ".json":
        return _json_sheets(path)
    if suffix == ".parquet":
        return _parquet_sheets(path)
    raise ProbeError("input_unsupported")


def input_file(directory: str) -> str:
    names = [n for n in os.listdir(directory) if n.startswith("data")]
    if len(names) != 1:
        raise ProbeError("input_missing")
    return os.path.join(directory, names[0])


# --- Output ------------------------------------------------------------------------------------------------------

def output_strings(doc: dict):
    """(location, text) of every string-like piece of the result: cells, column and table names, metric keys."""
    for t, table in enumerate(doc.get("tables") or []):
        if not isinstance(table, dict):
            continue
        if isinstance(table.get("name"), str):
            yield f"t{t}.name", table["name"]
        for c, col in enumerate(table.get("columns") or []):
            if isinstance(col, str):
                yield f"t{t}.col{c}", col
        for r, row in enumerate(table.get("rows") or []):
            for c, cell in enumerate(row if isinstance(row, list) else []):
                if isinstance(cell, str):  # Concept: string cells; numbers are covered by check 5 and the review
                    yield f"t{t}.r{r}.c{c}", cell
    metrics = doc.get("metrics")
    for m, key in enumerate(metrics if isinstance(metrics, dict) else {}):
        yield f"m{m}", key


def output_rows(doc: dict) -> list[tuple[str, frozenset]]:
    rows = []
    for t, table in enumerate(doc.get("tables") or []):
        for r, row in enumerate((table.get("rows") or []) if isinstance(table, dict) else []):
            if isinstance(row, list):
                values = frozenset(v for v in (norm(c) for c in row) if v is not None)
                rows.append((f"t{t}.r{r}", values))
    return rows


# --- Checks ------------------------------------------------------------------------------------------------------

def _looks_numeric(text: str) -> bool:
    return bool(NUMBER.match(text)) or bool(re.match(r"^\d{4}-\d{2}-\d{2}", text))


class Column:
    def __init__(self, name):
        self.name = norm(name) or ""
        self.values: set[str] = set()
        self.filled = 0
        self.numeric = 0
        self.patterned = 0

    def add(self, text: str, written: str) -> None:
        self.filled += 1
        self.values.add(text)
        if _looks_numeric(text):
            self.numeric += 1
        if any(p.match(written) for p in PATTERNS):
            self.patterned += 1

    def identifier(self, rows: int) -> bool:
        if NAME_HINT.search(self.name):
            return True
        if self.filled and self.patterned * 2 >= self.filled:
            return True
        text_column = self.filled and self.numeric * 2 < self.filled
        return bool(text_column and rows >= 2 and len(self.values) * 2 > rows)


def _row_threshold(width: int) -> int:
    return width - 1 if width >= 6 else width


def probe(input_dir: str, result_path: str) -> dict:
    with open(result_path, "rb") as f:
        try:
            doc = json.loads(f.read().decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise ProbeError("result_unreadable") from e
    if not isinstance(doc, dict):
        raise ProbeError("result_unreadable")
    out_rows = output_rows(doc)
    union: dict[str, list[int]] = {}
    for i, (_loc, values) in enumerate(out_rows):
        for v in values:
            union.setdefault(v, []).append(i)
    raw_hits: set[str] = set()
    columns_total, rows_total, identifier_values, identifier_columns = 0, 0, set(), 0
    for rows in sheets(input_file(input_dir)):
        header, columns, count = None, [], 0
        for row in rows:
            cells = list(row) if row is not None else []
            if header is None:
                header = cells
                columns = [Column(h) for h in header]
                continue
            count += 1
            values = set()
            for i, cell in enumerate(cells):
                text = norm(cell)
                if text is None:
                    continue
                values.add(text)
                while i >= len(columns):
                    columns.append(Column(None))
                columns[i].add(text, " ".join(str(cell).split()).casefold())
            if len(values) < 2:
                continue
            shared = [v for v in values if v in union]
            need = _row_threshold(len(values))
            if len(shared) < need:
                continue
            seen: dict[int, int] = {}
            for v in shared:
                for j in union[v]:
                    seen[j] = seen.get(j, 0) + 1
            raw_hits.update(out_rows[j][0] for j, n in seen.items() if n >= need)
        rows_total += count
        columns_total += len(columns)
        for col in columns:
            if col.identifier(count):
                identifier_columns += 1
                for v in col.values:
                    identifier_values |= variants(v)
    id_hits = []
    for loc, value in output_strings(doc):
        text = norm(value)
        if text is not None and windows(text) & identifier_values:
            id_hits.append(loc)
    return {"format": FORMAT, "error": None, "rows": rows_total, "columns": columns_total,
            "identifier_columns": identifier_columns,
            "identifier_hits": id_hits[:MAX_HITS], "identifier_hit_count": len(id_hits),
            "raw_row_hits": sorted(raw_hits)[:MAX_HITS], "raw_row_hit_count": len(raw_hits)}


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.environ.get("TBR_OUTPUT_DIR", "out")
    try:
        report = probe(os.environ.get("TBR_INPUT", "in"), os.path.join(here, "result.json"))
    except ProbeError as e:
        report = {"format": FORMAT, "error": str(e)}
    except Exception as e:  # noqa: BLE001 – a parser failure fails the checks (closed), never silently passes
        report = {"format": FORMAT, "error": "probe_failed", "type": type(e).__name__}
    with open(os.path.join(out_dir, "probe.json"), "w", encoding="utf-8") as f:
        json.dump(report, f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
