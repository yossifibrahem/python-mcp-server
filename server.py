"""Python Code Runner MCP Server"""

import time
from typing import List, Optional

from mcp.server.fastmcp import FastMCP

from security import SecurityError, check_code
from runner import PersistentWorker, format_result, pip_install

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT     = 120

mcp     = FastMCP("python_runner_mcp")
_worker = PersistentWorker()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

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
        check_code(code)
    except SecurityError as exc:
        return f"Blocked: {exc}"

    t0      = time.perf_counter()
    result  = await _worker.run(code, timeout, env_vars or {})
    elapsed = round(time.perf_counter() - t0, 3)

    return format_result(result, elapsed)


@mcp.tool(name="pip_install", annotations={
    "title": "Install Python Packages",
    "readOnlyHint": False, "destructiveHint": False,
    "idempotentHint": True, "openWorldHint": True,
})
async def pip_install_tool(
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
    result = pip_install(packages, upgrade)

    if result["timed_out"]:
        return "install timed out after 180s."
    if result["success"]:
        return f"installed: {', '.join(packages)} ({result['elapsed_s']}s)"

    error = result["stderr"].strip() or result["stdout"].strip() or "unknown error"
    return f"install failed: {error}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()