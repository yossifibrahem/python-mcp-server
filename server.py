"""
Python Runner MCP Server
Exposes a single tool: run_python — executes arbitrary Python code in a sandboxed subprocess.
"""

import asyncio
import json
import sys
import tempfile
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict

# ---------------------------------------------------------------------------
# Server init
# ---------------------------------------------------------------------------
mcp = FastMCP("python_runner_mcp")

# Execution limits
DEFAULT_TIMEOUT: int = 30   # seconds
MAX_OUTPUT_CHARS: int = 10_000


# ---------------------------------------------------------------------------
# Input model
# ---------------------------------------------------------------------------
class RunPythonInput(BaseModel):
    """Input for the run_python tool."""

    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )

    code: str = Field(
        ...,
        description="Python source code to execute. May be multi-line.",
        min_length=1,
    )
    timeout: Optional[int] = Field(
        default=DEFAULT_TIMEOUT,
        description=f"Maximum execution time in seconds (default {DEFAULT_TIMEOUT}, max 120).",
        ge=1,
        le=120,
    )
    stdin: Optional[str] = Field(
        default=None,
        description="Optional string to pass as standard input to the script.",
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _truncate(text: str, label: str) -> str:
    if len(text) > MAX_OUTPUT_CHARS:
        kept = text[:MAX_OUTPUT_CHARS]
        dropped = len(text) - MAX_OUTPUT_CHARS
        return f"{kept}\n… [{label} truncated — {dropped} chars omitted]"
    return text


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------
@mcp.tool(
    name="run_python",
    annotations={
        "title": "Run Python Script",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def run_python(params: RunPythonInput) -> str:
    """Execute Python code in an isolated subprocess and return stdout, stderr, and exit code.

    Use for computation, transformation, and logic that is faster and more reliable to run than to reason through manually

    The code runs in a temporary file using the same Python interpreter as this server
    (sys.executable). Output is capped at 10 000 characters per stream.

    Args:
        params (RunPythonInput): Validated input containing:
            - code (str): Python source code to run.
            - timeout (int): Max execution time in seconds (1-120, default 30).
            - stdin (Optional[str]): Data to feed to the script via stdin.

    Returns:
        str: JSON object with fields:
            - stdout (str): Captured standard output.
            - stderr (str): Captured standard error.
            - exit_code (int): Process exit code (0 = success).
            - timed_out (bool): True if execution was killed by the timeout.
            - error (str | null): High-level error message, if any.
    """
    result = {
        "stdout": "",
        "stderr": "",
        "exit_code": -1,
        "timed_out": False,
        "error": None,
    }

    # Write code to a temp file so tracebacks show a real path
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(params.code)
        tmp_path = tmp.name

    try:
        stdin_bytes = params.stdin.encode() if params.stdin else None

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            tmp_path,
            stdin=asyncio.subprocess.PIPE if stdin_bytes else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes),
                timeout=params.timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            result["timed_out"] = True
            result["error"] = (
                f"Execution timed out after {params.timeout} seconds and was terminated."
            )
            return json.dumps(result, indent=2)

        result["stdout"] = _truncate(stdout_b.decode(errors="replace"), "stdout")
        result["stderr"] = _truncate(stderr_b.decode(errors="replace"), "stderr")
        result["exit_code"] = proc.returncode

    except Exception as exc:  # noqa: BLE001
        result["error"] = f"Failed to launch subprocess: {exc}"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()  # stdio transport by default
