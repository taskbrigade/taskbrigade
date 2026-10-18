"""Trusted, standalone sandbox entry point. Never import this to parse uploads in the web process.

Only bounded CSV/XLSX inputs; openpyxl uses defusedxml. Samples exclude every nonempty source cell,
including coincidental matches. Fingerprints describe the script-facing structure, not row statistics.
"""
import csv
import hashlib
import io
import json
import math
import os
import random
import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import ZipFile

VERSION = "1.0"
MAX_ROWS, MAX_COLUMNS, MAX_SHEETS = 200000, 500, 50
MAX_UNPACKED = 512 * 1024 * 1024


def fingerprint(schema):
    structure = [{"name": s["name"], "columns": [
        {k: c[k] for k in ("name", "type", "precision") if k in c}
        for c in s["columns"]]} for s in schema["sheets"]]
    return hashlib.sha256(json.dumps(structure, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode()).hexdigest()


def read_tables(path):
    if path.suffix.lower() in (".csv", ".tsv"):
        with path.open(encoding="utf-8-sig", newline="") as f:
            probe = f.read(8192)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(probe, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel_tab if path.suffix == ".tsv" else csv.excel
            yield "Sheet1", bounded_rows(csv.reader(f, dialect))
    elif path.suffix.lower() == ".xlsx":
        import defusedxml.ElementTree  # noqa: F401 – required before parsing untrusted XML
        import openpyxl
        if not openpyxl.xml.DEFUSEDXML:
            raise ValueError("defusedxml required")
        with ZipFile(path) as z:
            if sum(f.file_size for f in z.infolist()) > MAX_UNPACKED or len(z.infolist()) > 10000:
                raise ValueError("workbook expansion limit")
        book = openpyxl.load_workbook(path, read_only=True, data_only=True, keep_links=False)
        try:
            if len(book.worksheets) > MAX_SHEETS:
                raise ValueError("sheet limit")
            for sheet in book.worksheets:
                yield sheet.title, bounded_rows(sheet.iter_rows(values_only=True))
        finally:
            book.close()
    else:
        raise ValueError("unsupported format")


def header_names(row):
    return [str(v) if v is not None else f"Column{i + 1}" for i, v in enumerate(row)]


def bounded_rows(rows):
    out = []
    for row in rows:
        if len(out) > MAX_ROWS or len(row) > MAX_COLUMNS:
            raise ValueError("table limit")
        out.append(list(row))
    if not out or not out[0]:
        raise ValueError("empty table")
    names = header_names(out[0])
    if len(set(names)) != len(names) or any(len(n) > 200 for n in names):
        raise ValueError("ambiguous headers")
    if any(len(row) > len(names) for row in out[1:]):
        raise ValueError("extra cells")
    return [names] + [row + [None] * (len(names) - len(row)) for row in out[1:]]


def cell_text(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else str(value)


def identity(value):
    """Exclude equivalent representations too (12.50 == 12.5; midnight Excel datetime == date)."""
    text = cell_text(value)
    if kind(value) == "date":
        return ("date", text[:10])
    try:
        number = Decimal(text)
        if number.is_finite():
            return ("number", number.normalize())
    except InvalidOperation:
        pass
    return ("text", text)


def kind(value):
    if isinstance(value, (date, datetime)) or re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T].*)?", str(value)):
        return "date"
    try:
        number = float(value)
        if not math.isfinite(number):
            return "string"
        return "integer" if number.is_integer() and "." not in str(value) else "decimal"
    except (ValueError, TypeError):
        return "string"


def column(name, cells, k):
    values = [v for v in cells if v is not None and v != ""]
    counts = Counter(cell_text(v) for v in values)
    kinds = {kind(v) for v in values}
    typ = (next(iter(kinds)) if len(kinds) == 1 else
           "decimal" if kinds and kinds <= {"integer", "decimal"} else "string")
    col = {"name": name, "type": typ, "nulls": round(1 - len(values) / len(cells), 4) if cells else 0,
           "cardinality": len(counts)}
    pii_name = re.search(r"name|kunde|client|customer|mandant|patient|iban|email|e-mail|phone|telefon|tax|steuer|"
                         r"(?:^|_)(?:id|identifier|kennung)(?:$|_)", name, re.I)
    pii_value = any(re.search(r"@|\b[A-Z]{2}\d{2}[ A-Z0-9]{10,}\b|^(?:\+|00|0)[\d ()/-]{7,}$|^\d{11}$", v)
                    for v in counts)
    identifier = bool(pii_name or pii_value or (typ == "string" and len(counts) > len(cells) / 2))
    if identifier:
        col["role"] = "identifier"
        if pii_name or pii_value:
            col["pii"] = ("email" if re.search(r"email|e-mail", name, re.I) or any("@" in v for v in counts)
                          else "iban" if re.search(r"iban", name, re.I) else "possible")
        return col
    if typ in ("integer", "decimal") and values:
        nums = [float(v) for v in values]
        scale = 10 ** math.floor(math.log10(max(abs(v) for v in nums) or 1))
        col["range"] = {"min": math.floor(min(nums) / scale) * scale,
                        "max": math.ceil(max(nums) / scale) * scale}
        if typ == "decimal":
            col["precision"] = min(6, max(len(str(v).partition(".")[2]) for v in values))
    elif typ == "date" and values:
        months = sorted(cell_text(v)[:7] for v in values)
        col["range"] = {"min": months[0], "max": months[-1]}
    elif len(counts) <= 20 and counts:
        col["role"] = "category"
        col["values"] = sorted(v for v, n in counts.items() if n >= k)
        if any(n < k for n in counts.values()):
            col["values"].append("(rare)")
    return col


def sample(schema, forbidden, seed):
    rng = random.Random(str(seed))
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    # One block per sheet, labelled by synthetic sheet index, never by source cell values.
    for sheet in schema["sheets"]:
        writer.writerow([c["name"] for c in sheet["columns"]])
        for row in range(20):
            values = []
            for col in sheet["columns"]:
                value = ""
                if rng.random() >= col["nulls"]:
                    for attempt in range(len(forbidden) + 100):
                        n = rng.randrange(1000000) + attempt
                        if col["type"] in ("integer", "decimal"):
                            bounds = col.get("range", {"min": 1000000, "max": 9999999})
                            num = rng.uniform(bounds["min"], bounds["max"]) + attempt
                            value = str(round(num, col.get("precision", 0))) if col["type"] == "decimal" else str(int(num))
                        elif col["type"] == "date":
                            bounds = col.get("range", {"min": "2100-01", "max": "2100-12"})
                            start = date.fromisoformat(bounds["min"] + "-01")
                            end = date.fromisoformat(bounds["max"] + "-28")
                            value = date.fromordinal(start.toordinal() + rng.randrange(
                                (end - start).days + 1) + attempt).isoformat()
                        else:
                            value = (f"synthetic{n}@example.invalid" if col.get("pii") == "email" else
                                     f"DE00 0000 0000 00{n:010d}" if col.get("pii") == "iban" else
                                     f"ID-{n:07d}" if col.get("role") == "identifier" else f"Synthetic {n}")
                        if identity(value) not in forbidden:
                            break
                    else:
                        raise ValueError("sample generation exhausted")
                values.append(value)
            writer.writerow(values)
    return out.getvalue()


def profile(path, dataset, k=5):
    sheets, forbidden = [], set()
    for name, rows in read_tables(path):
        headers, cells = rows[0], rows[1:]
        forbidden.update(identity(v) for row in cells for v in row if v is not None and v != "")
        sheets.append({"name": name, "rows": len(cells), "columns": [
            column(header, [r[i] for r in cells], k) for i, header in enumerate(headers)]})
    schema = {"format": "tbr-schema/1", "dataset": dataset, "sheets": sheets}
    return schema, sample(schema, forbidden, dataset), fingerprint(schema)


def alias_input(config):
    """Called only inside a run sandbox; retain the source mount read-only."""
    import tempfile
    mapping = config["aliases"]
    original = config["original"]
    source = next(Path(os.environ["TBR_INPUT"]).glob("data.*"))
    target = Path(tempfile.mkdtemp(prefix="tbr-alias-")) / source.name
    if source.suffix in (".csv", ".tsv"):
        with source.open(encoding="utf-8-sig", newline="") as f:
            probe = f.read(8192)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(probe, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel_tab if source.suffix == ".tsv" else csv.excel
            rows = csv.reader(f, dialect)
            names = next(rows)
            expected = [c["name"] for c in original["sheets"][0]["columns"]]
            if names != expected:
                raise SystemExit(3)
            with target.open("w", encoding="utf-8", newline="") as out:
                writer = csv.writer(out, dialect)
                writer.writerow([mapping.get(f"s0c{j}", name) for j, name in enumerate(names)])
                writer.writerows(rows)
    elif source.suffix == ".xlsx":
        import openpyxl
        # Validate size/shape with the same bounded parser before opening writable cells.
        list(read_tables(source))
        book = openpyxl.load_workbook(source, keep_links=False, data_only=True)
        expected = original["sheets"]
        if [s.title for s in book] != [s["name"] for s in expected]:
            raise SystemExit(3)
        final_names = [mapping.get(f"s{i}", s["name"]) for i, s in enumerate(expected)]
        prefix = "tbr-temporary-"
        while any(f"{prefix}{i}" in final_names + book.sheetnames for i in range(len(expected))):
            prefix += "_"
        for i, sheet in enumerate(book):
            names = [c["name"] for c in expected[i]["columns"]]
            if header_names([c.value for c in sheet[1]]) != names:
                raise SystemExit(3)
            sheet.title = f"{prefix}{i}"
            for j, name in enumerate(names):
                cell = sheet.cell(1, j + 1, mapping.get(f"s{i}c{j}", name))
                cell.data_type = "s"
        for i, sheet in enumerate(book):
            sheet.title = mapping.get(f"s{i}", expected[i]["name"])
        book.save(target)
        book.close()
    else:
        raise SystemExit(3)
    os.environ["TBR_INPUT"] = str(target.parent)


def main():
    config = json.loads((Path(__file__).parent / "profile-config.json").read_text())
    inputs = list(Path(os.environ["TBR_INPUT"]).glob("data.*"))
    schema, rows, sha = profile(inputs[0], config["dataset"], config["k"])
    out = Path(os.environ["TBR_OUTPUT_DIR"])
    (out / "schema.json").write_text(json.dumps(schema, ensure_ascii=True), encoding="utf-8")
    (out / "sample.csv").write_text(rows, encoding="utf-8")
    (out / "result.json").write_text(json.dumps({"format": "tbr-profile/1", "schema_fingerprint": sha,
                                               "profiler_version": VERSION}), encoding="utf-8")


if __name__ == "__main__":
    main()
