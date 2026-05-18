from __future__ import annotations

import ast
import importlib
from dataclasses import dataclass
from types import ModuleType

ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {
        "json",
        "asyncio",
        "math",
        "statistics",
        "collections",
        "re",
        "datetime",
        "csv",
        "io",
        "base64",
        "html",
        "urllib",
        "urllib.parse",
        "mcp",  # injected shim
    }
)

# Builtins that would let a script escape the sandbox (filesystem / subprocess /
# arbitrary imports). The runner replaces the real builtins module with a
# filtered copy that omits these names.
BLOCKED_BUILTINS: frozenset[str] = frozenset(
    {
        "__import__",
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "breakpoint",
        "memoryview",
    }
)

DANGEROUS_ATTRS: frozenset[str] = frozenset(
    {"__class__", "__bases__", "__subclasses__", "__globals__", "__builtins__", "__import__"}
)


@dataclass
class PolicyViolation(Exception):
    reason: str
    node: str = ""

    def __str__(self) -> str:  # type: ignore[override]
        return f"PolicyViolation: {self.reason}" + (f" at `{self.node}`" if self.node else "")


def _root(module: str) -> str:
    return module.split(".", 1)[0]


def check_script(source: str) -> None:
    """Raises PolicyViolation if the script uses disallowed imports/attrs.

    AST-level enforcement is the first layer. The runtime layer (filtered
    builtins, `python -I`) is the second. Together they block the common
    escape patterns: arbitrary import, getattr(__builtins__, ...), eval/exec.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise PolicyViolation(f"syntax error: {e}") from e

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _root(alias.name) not in ALLOWED_IMPORTS:
                    raise PolicyViolation(f"disallowed import: {alias.name}", alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is None or _root(node.module) not in ALLOWED_IMPORTS:
                raise PolicyViolation(f"disallowed from-import: {node.module}", node.module or "")
        elif isinstance(node, ast.Attribute):
            if node.attr in DANGEROUS_ATTRS:
                raise PolicyViolation(f"dangerous attribute access: {node.attr}", node.attr)
        elif isinstance(node, ast.Name):
            if node.id in BLOCKED_BUILTINS:
                raise PolicyViolation(f"blocked builtin: {node.id}", node.id)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BLOCKED_BUILTINS:
                raise PolicyViolation(f"blocked call: {func.id}", func.id)


def filtered_builtins() -> dict:
    """Return a builtins dict with dangerous names stripped, used to seed the
    sandbox execution namespace."""
    import builtins as _b

    safe: dict[str, object] = {}
    for name in dir(_b):
        if name.startswith("__"):
            continue
        if name in BLOCKED_BUILTINS:
            continue
        safe[name] = getattr(_b, name)
    # Keep these even though they start with __ — they're load-bearing.
    safe["__name__"] = "__sandbox__"
    safe["__builtins__"] = safe
    safe["__import__"] = safe_import
    return safe


def safe_import(
    name: str,
    globals: dict | None = None,  # noqa: A002 - matches __import__ signature
    locals: dict | None = None,  # noqa: A002 - matches __import__ signature
    fromlist: tuple | list = (),
    level: int = 0,
) -> ModuleType:
    """Import only modules that passed the AST allowlist.

    The sandbox still needs an import function for ordinary statements like
    `import json`. Keeping this as a tiny allowlisted wrapper preserves the
    runtime half of the sandbox while avoiding a blanket `__import__`.
    """
    if level != 0:
        raise ImportError("relative imports are not allowed in the sandbox")

    root = _root(name)
    if root not in ALLOWED_IMPORTS:
        raise ImportError(f"import is not allowed in the sandbox: {name}")
    module = importlib.import_module(name)
    if not fromlist and "." in name:
        return importlib.import_module(root)
    return module
