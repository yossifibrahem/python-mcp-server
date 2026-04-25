"""Python Code Runner MCP Server"""

import ast
import sys
import json
import subprocess
import tempfile
import os
import time
from typing import Optional, List

from mcp.server.fastmcp import FastMCP

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT     = 120
MAX_OUTPUT_LEN  = 20_000
PYTHON_BIN      = sys.executable

mcp = FastMCP("python_runner_mcp")

# Modules that provide OS-level access
BLOCKED_MODULES = {
    "os", "os.path", "sys", "subprocess", "shutil", "pathlib",
    "socket", "socketserver", "ctypes", "ctypes.util",
    "multiprocessing", "concurrent.futures",
    "tempfile", "glob", "fnmatch",
    "signal", "resource", "mmap",
    "pwd", "grp", "fcntl", "termios", "tty", "pty",
    "winreg", "winsound", "msvcrt",
    "importlib", "importlib.util", "importlib.machinery",
    "builtins", "gc", "inspect", "dis",
}

# Built-in calls that bypass import restrictions
BLOCKED_BUILTINS = {"open", "exec", "eval", "compile", "__import__", "breakpoint"}


class SecurityError(ValueError):
    pass


def _check_code(code: str) -> None:
    """Parse code as an AST and raise SecurityError if any OS operations are detected."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise SecurityError(f"Syntax error: {exc}") from exc

    for node in ast.walk(tree):
        # Block: import os / import subprocess / ...
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if alias.name in BLOCKED_MODULES or top in BLOCKED_MODULES:
                    raise SecurityError(f"Import of '{alias.name}' is not allowed.")

        # Block: from os import ... / from pathlib import Path / ...
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = module.split(".")[0]
            if module in BLOCKED_MODULES or top in BLOCKED_MODULES:
                raise SecurityError(f"Import from '{module}' is not allowed.")

        # Block: open(...) / exec(...) / eval(...) / __import__(...) / ...
        elif isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in BLOCKED_BUILTINS:
                raise SecurityError(f"Use of '{name}' is not allowed.")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_LEN:
        return text
    half = MAX_OUTPUT_LEN // 2
    return text[:half] + f"\n\n... [truncated] ...\n\n" + text[-half:]


def _run_code(code: str, timeout: int, env_vars: dict) -> str:
    if not (1 <= timeout <= MAX_TIMEOUT):
        return f"Invalid timeout: must be between 1 and {MAX_TIMEOUT} seconds."

    try:
        _check_code(code)
    except SecurityError as exc:
        return f"Blocked: {exc}"

    env = {**os.environ, **env_vars}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        tmp = f.name
    try:
        t0 = time.perf_counter()
        proc = subprocess.run(
            [PYTHON_BIN, tmp], capture_output=True, text=True, timeout=timeout, env=env
        )
        elapsed = round(time.perf_counter() - t0, 3)
        status = "Success" if proc.returncode == 0 else "Failed"
        parts = [f"{status} (exit {proc.returncode}, {elapsed}s)"]
        if proc.stdout: parts += ["\nstdout:", _truncate(proc.stdout)]
        if proc.stderr: parts += ["\nstderr:", _truncate(proc.stderr)]
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s."
    except Exception as exc:
        return f"Error: {exc}"
    finally:
        os.unlink(tmp)



# ── Helpers (pip) ──────────────────────────────────────────────────────────────

def _pip_install(packages: List[str], upgrade: bool = False) -> dict:
    """Run pip install and return a structured result dict."""
    cmd = [PYTHON_BIN, "-m", "pip", "install", "--quiet"]
    if upgrade:
        cmd.append("--upgrade")
    cmd += packages

    try:
        t0 = time.perf_counter()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        elapsed = round(time.perf_counter() - t0, 3)
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "elapsed_s": elapsed,
            "stdout": _truncate(proc.stdout),
            "stderr": _truncate(proc.stderr),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "exit_code": -1, "elapsed_s": 180,
                "stdout": "", "stderr": "", "timed_out": True}
    except Exception as exc:
        return {"success": False, "exit_code": -1, "elapsed_s": 0,
                "stdout": "", "stderr": str(exc), "timed_out": False}


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool(name="pip_install", annotations={
    "title": "Install Python Packages",
    "readOnlyHint": False, "destructiveHint": False,
    "idempotentHint": True, "openWorldHint": True,
})
async def pip_install(
    packages: List[str],
    upgrade: bool = False,
) -> str:
    """Install one or more pip packages into the active Python environment.

    Args:
        packages (List[str]): pip specifiers, e.g. ['numpy', 'pandas>=2', 'requests==2.31'].
        upgrade (bool, optional): Re-install / upgrade already-present packages (default False).

    Returns:
        str: Single line — "installed: <packages> (<elapsed>s)" on success,
             "install failed: <error>" on failure, or "install timed out" if pip exceeded 180s.
    """
    result = _pip_install(packages, upgrade)
    if result["timed_out"]:
        return "install timed out after 180s."
    if result["success"]:
        return f"installed: {', '.join(packages)} ({result['elapsed_s']}s)"
    error = result["stderr"].strip() or result["stdout"].strip() or "unknown error"
    return f"install failed: {error}"


@mcp.tool(name="python_run", annotations={
    "title": "Run Python Code",
    "readOnlyHint": False, "destructiveHint": False,
    "idempotentHint": False, "openWorldHint": True,
})
async def python_run(
    code: str,
    timeout: int = DEFAULT_TIMEOUT,
    env_vars: Optional[dict] = None,
) -> str:
    """Execute Python code in an isolated subprocess and return stdout/stderr.

    Args:
        code (str): Python source code to run.
        timeout (int, optional): Kill after N seconds (default 30, max 120).
        env_vars (dict, optional): Extra environment variables.

    Returns:
        str: Status, exit code, elapsed time, stdout, and stderr.
    """
    return _run_code(code, timeout, env_vars or {})


if __name__ == "__main__":
    mcp.run()