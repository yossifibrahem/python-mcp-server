"""
Security layer for python_runner_mcp.

Performs AST-level static analysis on submitted code before it reaches the
worker process. Blocked code is never executed.
"""

import ast
import os
import sys


# ---------------------------------------------------------------------------
# Blocked sets
# ---------------------------------------------------------------------------

#: Modules that expose OS-level access. Configurable via ALLOW_MODULES.
_DEFAULT_BLOCKED_MODULES: frozenset[str] = frozenset({
    "os", "os.path",
    "sys",
    "subprocess",
    "shutil",
    "pathlib",
    "socket", "socketserver",
    "ctypes", "ctypes.util",
    "multiprocessing", "concurrent.futures",
    "tempfile",
    "glob", "fnmatch",
    "signal", "resource", "mmap",
    "pwd", "grp", "fcntl", "termios", "tty", "pty",
    "winreg", "winsound", "msvcrt",
    "importlib", "importlib.util", "importlib.machinery",
    "builtins", "gc", "inspect", "dis",
})

#: Built-in calls that bypass import restrictions. Never configurable.
BLOCKED_BUILTINS: frozenset[str] = frozenset({
    "open", "exec", "eval", "compile", "__import__", "breakpoint",
})


# ---------------------------------------------------------------------------
# Module allowlist (read once at startup from the environment)
# ---------------------------------------------------------------------------

def _build_blocked_modules() -> frozenset[str]:
    """
    Derive the effective blocked-module set from the environment.

    Modules listed in ALLOW_MODULES (comma-separated) are removed from the
    default block list. Allowing a top-level name also unblocks its
    sub-modules, so ``ALLOW_MODULES=os`` implicitly allows ``os.path``.

    Logs the effective lists to stderr when any modules are allowed.
    """
    raw = os.environ.get("ALLOW_MODULES", "")
    allowed = {m.strip() for m in raw.split(",") if m.strip()}

    if not allowed:
        return _DEFAULT_BLOCKED_MODULES

    effective = frozenset(
        mod for mod in _DEFAULT_BLOCKED_MODULES
        if mod not in allowed and mod.split(".")[0] not in allowed
    )

    print(
        f"[python_runner] Modules allowed : {sorted(allowed)}\n"
        f"[python_runner] Modules blocked : {sorted(effective)}",
        file=sys.stderr,
    )
    return effective


#: Effective blocked-module set for this server process.
BLOCKED_MODULES: frozenset[str] = _build_blocked_modules()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class SecurityError(ValueError):
    """Raised when submitted code violates the security policy."""


def check_code(code: str) -> None:
    """
    Parse *code* as an AST and raise :exc:`SecurityError` if it contains
    any blocked imports or built-in calls.

    Raises :exc:`SecurityError` also on syntax errors so callers only need
    to handle one exception type.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise SecurityError(f"Syntax error: {exc}") from exc

    for node in ast.walk(tree):
        _check_node(node)


def _check_node(node: ast.AST) -> None:
    """Inspect a single AST node and raise SecurityError if it is blocked."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            _assert_module_allowed(alias.name)

    elif isinstance(node, ast.ImportFrom):
        _assert_module_allowed(node.module or "")

    elif isinstance(node, ast.Call):
        name = _call_name(node)
        if name in BLOCKED_BUILTINS:
            raise SecurityError(f"Use of '{name}' is not allowed.")


def _assert_module_allowed(module: str) -> None:
    top = module.split(".")[0]
    if module in BLOCKED_MODULES or top in BLOCKED_MODULES:
        raise SecurityError(f"Import of '{module}' is not allowed.")


def _call_name(node: ast.Call) -> str | None:
    """Return the bare name of a Call node's function, or None."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None