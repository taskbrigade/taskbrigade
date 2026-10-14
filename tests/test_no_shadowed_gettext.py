"""Schutztest: `_` ist die Übersetzungsfunktion (app.i18n). Wer in derselben Funktion `_` als Variable belegt
(z. B. `a, _, b = s.partition(" ")`), macht jeden `_()`-Aufruf dort zum TypeError."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
NESTED = SCOPES + (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.ClassDef)


def _own_nodes(fn):
    """Knoten im eigenen Scope der Funktion (ohne verschachtelte Funktionen/Comprehensions)."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, NESTED):
            stack.extend(ast.iter_child_nodes(node))


def _offenders(tree):
    for fn in ast.walk(tree):
        if not isinstance(fn, SCOPES):
            continue
        nodes = list(_own_nodes(fn))
        binds = any(isinstance(n, ast.Name) and n.id == "_" and isinstance(n.ctx, ast.Store) for n in nodes)
        binds |= any(a.arg == "_" for a in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs)
        calls = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_" for n in nodes)
        if binds and calls:
            yield getattr(fn, "name", "<lambda>"), fn.lineno


def test_gettext_underscore_is_never_shadowed():
    found = []
    for path in sorted((ROOT / "app").rglob("*.py")) + sorted((ROOT / "cli").rglob("*.py")):
        for name, line in _offenders(ast.parse(path.read_text())):
            found.append(f"{path.relative_to(ROOT)}:{line} {name}")
    assert not found, "`_` als Variable überschreibt die Übersetzungsfunktion: " + ", ".join(found)


def test_detector_finds_the_original_bug():
    bad = ast.parse('def f(h):\n    a, _, b = h.partition(" ")\n    return _("x")\n')
    ok = ast.parse('def g(p):\n    return _("x"), [n for n, _, _ in p]\n')
    assert list(_offenders(bad)) == [("f", 1)] and list(_offenders(ok)) == []
