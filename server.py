"""Python Code Runner MCP Server"""

import ast
import sys
import json
import subprocess
import os
import time
import asyncio
from pathlib import Path
from typing import Optional, List

from mcp.server.fastmcp import FastMCP

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT     = 120
MAX_OUTPUT_LEN  = 20_000
PYTHON_BIN      = sys.executable
WORKER_SCRIPT   = str(Path(__file__).parent / "worker.py")

mcp = FastMCP("python_runner_mcp")

# ---------------------------------------------------------------------------
# Security — same rules as before
# ---------------------------------------------------------------------------

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
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if alias.name in BLOCKED_MODULES or top in BLOCKED_MODULES:
                    raise SecurityError(f"Import of '{alias.name}' is not allowed.")

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = module.split(".")[0]
            if module in BLOCKED_MODULES or top in BLOCKED_MODULES:
                raise SecurityError(f"Import from '{module}' is not allowed.")

        elif isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in BLOCKED_BUILTINS:
                raise SecurityError(f"Use of '{name}' is not allowed.")


# ---------------------------------------------------------------------------
# Persistent worker — one long-lived Python process reused across all calls
# ---------------------------------------------------------------------------

class PersistentWorker:
    """
    Wraps a long-running worker.py subprocess.

    Code is sent as a JSON line over stdin; the result arrives as a JSON line
    on stdout.  A per-call asyncio timeout kills and restarts the worker if
    it stops responding (e.g. infinite loop or segfault).

    A single asyncio.Lock serialises concurrent tool calls so we never
    interleave requests/responses on the same pipe.
    """

    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()

    async def _start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            PYTHON_BIN, WORKER_SCRIPT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,   # worker errors go nowhere — keep pipes clean
        )

    async def _ensure_alive(self) -> None:
        if self._proc is None or self._proc.returncode is not None:
            await self._start()

    async def _kill_and_reset(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                await self._proc.wait()
            except ProcessLookupError:
                pass
            self._proc = None

    async def run(self, code: str, timeout: int, env_vars: dict) -> dict:
        """Send *code* to the worker and return {"ok", "stdout", "stderr"}."""
        async with self._lock:
            await self._ensure_alive()

            request = json.dumps({"code": code, "env_vars": env_vars}) + "\n"
            try:
                self._proc.stdin.write(request.encode())
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # Worker died between calls — restart and retry once
                await self._kill_and_reset()
                await self._start()
                self._proc.stdin.write(request.encode())
                await self._proc.stdin.drain()

            try:
                raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._kill_and_reset()
                return {"ok": False, "stdout": "", "stderr": f"Timed out after {timeout}s."}
            except Exception as exc:  # noqa: BLE001
                await self._kill_and_reset()
                return {"ok": False, "stdout": "", "stderr": f"Worker error: {exc}"}

            if not raw:
                # EOF — worker crashed
                await self._kill_and_reset()
                return {"ok": False, "stdout": "", "stderr": "Worker exited unexpectedly."}

            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                return {"ok": False, "stdout": "", "stderr": f"Bad worker response: {exc}"}


# Module-level singleton — created once per server process
_worker = PersistentWorker()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_LEN:
        return text
    half = MAX_OUTPUT_LEN // 2
    return text[:half] + "\n\n... [truncated] ...\n\n" + text[-half:]


def _format_result(result: dict, elapsed: float) -> str:
    status = "Success" if result["ok"] else "Failed"
    parts = [f"{status} ({elapsed}s)"]
    if result.get("stdout"):
        parts += ["\nstdout:", _truncate(result["stdout"])]
    if result.get("stderr"):
        parts += ["\nstderr:", _truncate(result["stderr"])]
    return "\n".join(parts)


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
            "elapsed_s": elapsed,
            "stdout": _truncate(proc.stdout),
            "stderr": _truncate(proc.stderr),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "elapsed_s": 180, "stdout": "", "stderr": "", "timed_out": True}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "elapsed_s": 0, "stdout": "", "stderr": str(exc), "timed_out": False}


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

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
        str: Status, elapsed time, stdout, and stderr.
    """
    if not (1 <= timeout <= MAX_TIMEOUT):
        return f"Invalid timeout: must be between 1 and {MAX_TIMEOUT} seconds."

    try:
        _check_code(code)
    except SecurityError as exc:
        return f"Blocked: {exc}"

    t0 = time.perf_counter()
    result = await _worker.run(code, timeout, env_vars or {})
    elapsed = round(time.perf_counter() - t0, 3)

    return _format_result(result, elapsed)


if __name__ == "__main__":
    mcp.run()