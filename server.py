"""Python Code Runner MCP Server"""

import sys
import json
import subprocess
import tempfile
import os
import time
from typing import Optional, List
from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT     = 120
MAX_OUTPUT_LEN  = 20_000
PYTHON_BIN      = sys.executable

mcp = FastMCP("python_runner_mcp")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_LEN:
        return text
    half = MAX_OUTPUT_LEN // 2
    return text[:half] + f"\n\n... [truncated] ...\n\n" + text[-half:]


def _run_code(code: str, timeout: int, env_vars: dict) -> str:
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


# ── Input models ───────────────────────────────────────────────────────────────

class RunCodeInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    code: str = Field(..., description="Python source code to execute.", min_length=1)
    timeout: Optional[int] = Field(default=DEFAULT_TIMEOUT, ge=1, le=MAX_TIMEOUT,
        description="Max execution time in seconds (default 30, max 120).")
    env_vars: Optional[dict] = Field(default_factory=dict,
        description="Extra environment variables (dict of str→str).")


class InstallPackagesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    packages: List[str] = Field(..., min_length=1,
        description="pip package specifiers to install (e.g. ['numpy', 'requests==2.31', 'pandas>=2']).")
    upgrade: Optional[bool] = Field(default=False,
        description="Pass --upgrade to pip so already-installed packages are upgraded.")


class RunWithPackagesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    code: str = Field(..., description="Python source code to execute.", min_length=1)
    packages: List[str] = Field(..., min_length=1,
        description="pip package specifiers to install first (e.g. ['numpy', 'requests==2.31']).")
    timeout: Optional[int] = Field(default=DEFAULT_TIMEOUT, ge=1, le=MAX_TIMEOUT,
        description="Execution timeout in seconds (install time is not counted).")


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

@mcp.tool(name="install_packages", annotations={
    "title": "Install Python Packages",
    "readOnlyHint": False, "destructiveHint": False,
    "idempotentHint": True, "openWorldHint": True,
})
async def install_packages(params: InstallPackagesInput) -> str:
    """Install one or more pip packages into the active Python environment.

    Uses the same interpreter that runs this MCP server, so installed packages
    are immediately importable in subsequent run_python calls.

    Args:
        params (InstallPackagesInput):
            - packages (List[str]): pip specifiers, e.g. ['numpy', 'pandas>=2', 'requests==2.31'].
            - upgrade (bool, optional): Re-install / upgrade already-present packages (default False).

    Returns:
        str: JSON object with fields:
            - success (bool): True if pip exited 0.
            - exit_code (int): pip's exit code.
            - elapsed_s (float): Seconds taken.
            - packages (List[str]): The specifiers that were requested.
            - stdout (str): pip output.
            - stderr (str): pip error output.
            - timed_out (bool): True if pip was killed by the 180 s hard limit.
    """
    result = _pip_install(params.packages, params.upgrade or False)
    result["packages"] = params.packages
    return json.dumps(result, indent=2)


@mcp.tool(name="python_run", annotations={
    "title": "Run Python Code",
    "readOnlyHint": False, "destructiveHint": False,
    "idempotentHint": False, "openWorldHint": True,
})
async def python_run(params: RunCodeInput) -> str:
    """Execute Python code in an isolated subprocess and return stdout/stderr.

    Args:
        params (RunCodeInput):
            - code (str): Python source code to run.
            - timeout (int, optional): Kill after N seconds (default 30, max 120).
            - env_vars (dict, optional): Extra environment variables.

    Returns:
        str: Status, exit code, elapsed time, stdout, and stderr.
    """
    return _run_code(params.code, params.timeout or DEFAULT_TIMEOUT, params.env_vars or {})


@mcp.tool(name="python_run_with_packages", annotations={
    "title": "Install Packages & Run Python Code",
    "readOnlyHint": False, "destructiveHint": False,
    "idempotentHint": False, "openWorldHint": True,
})
async def python_run_with_packages(params: RunWithPackagesInput) -> str:
    """pip-install packages then execute Python code, returning combined output.

    Args:
        params (RunWithPackagesInput):
            - code (str): Python source code to run.
            - packages (List[str]): pip specifiers to install first.
            - timeout (int, optional): Execution timeout in seconds.

    Returns:
        str: Install log followed by execution result.
    """
    pip = _pip_install(params.packages)
    ok = pip["success"]
    log = _truncate(pip["stdout"] + pip["stderr"])
    status = "ok" if ok else "failed"
    header = f"install {status}: {', '.join(params.packages)}\n{log or '(no output)'}"

    if not ok:
        return header
    return header + "\n\n" + _run_code(params.code, params.timeout or DEFAULT_TIMEOUT, {})


if __name__ == "__main__":
    mcp.run()