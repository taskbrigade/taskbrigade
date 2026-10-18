"""Static pre-check of a runner script (research/konzept-datenzonen.md §5 step 3).

Defence in depth only – the sandbox (no network, read-only system, one writable directory) is the boundary. The
check refuses imports and calls a data script never needs: network, subprocesses, native code, dynamic code. A hit
aborts the job before any data is decrypted; findings name the line and construct, never data.
"""
import ast
from dataclasses import dataclass

FORBIDDEN_MODULES = frozenset({
    "socket", "ssl", "http", "urllib", "urllib2", "urllib3", "requests", "httpx", "aiohttp", "ftplib", "smtplib",
    "telnetlib", "xmlrpc", "socketserver", "asyncio", "selectors", "select",
    "subprocess", "multiprocessing", "pty", "ctypes", "cffi", "importlib", "runpy", "code", "codeop", "pickle",
    "marshal", "shelve", "webbrowser", "paramiko", "asyncssh", "pip", "ensurepip", "site", "sysconfig",
})
FORBIDDEN_CALLS = frozenset({"exec", "eval", "compile", "__import__", "breakpoint", "globals", "vars"})
FORBIDDEN_ATTRS = {
    "os": frozenset({"system", "popen", "fork", "forkpty", "kill", "killpg", "putenv", "unsetenv",
                     "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
                     "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
                     "posix_spawn", "posix_spawnp", "startfile", "chdir", "chroot", "setuid", "setgid"}),
    "sys": frozenset({"settrace", "setprofile", "addaudithook", "path_hooks", "meta_path"}),
}


@dataclass(frozen=True)
class Finding:
    line: int
    what: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.what}"


def _module_root(name: str | None) -> str:
    return (name or "").split(".")[0]


class _Visitor(ast.NodeVisitor):
    def __init__(self):
        self.findings: list[Finding] = []
        self.aliases: dict[str, str] = {}  # local name → module root (`import os as o`)

    def _hit(self, node: ast.AST, what: str) -> None:
        self.findings.append(Finding(getattr(node, "lineno", 0), what))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = _module_root(alias.name)
            if root in FORBIDDEN_MODULES:
                self._hit(node, f"import {alias.name}")
            self.aliases[alias.asname or root] = root
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = _module_root(node.module)
        if node.level or root in FORBIDDEN_MODULES:
            self._hit(node, f"from {'.' * node.level}{node.module or ''} import …")
        for alias in node.names:
            if alias.name in FORBIDDEN_ATTRS.get(root, ()):
                self._hit(node, f"from {root} import {alias.name}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
            self._hit(node, f"{func.id}()")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name):
            root = self.aliases.get(node.value.id, node.value.id)
            if node.attr in FORBIDDEN_ATTRS.get(root, ()):
                self._hit(node, f"{root}.{node.attr}")
        if node.attr.startswith("__") and node.attr.endswith("__") and node.attr not in ("__name__", "__file__"):
            self._hit(node, f"dunder attribute {node.attr}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in ("__builtins__", "__loader__", "__spec__"):
            self._hit(node, node.id)
        self.generic_visit(node)


def check(source: str | bytes) -> list[Finding]:
    """Findings for a script; an unparsable script is one finding."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as e:
        return [Finding(getattr(e, "lineno", 0) or 0, "the script does not parse")]
    visitor = _Visitor()
    visitor.visit(tree)
    return sorted(set(visitor.findings), key=lambda f: (f.line, f.what))
