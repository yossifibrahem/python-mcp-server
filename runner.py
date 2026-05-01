"""
Execution back-ends for python_runner_mcp.

PersistentWorker  — long-lived worker subprocess for python_run calls.
pip_install       — thin wrapper around pip for package installation.
"""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import List

WORKER_SCRIPT = str(Path(__file__).parent / "worker.py")
PYTHON_BIN    = sys.executable
MAX_OUTPUT_LEN = 20_000


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def truncate(text: str) -> str:
    """Trim *text* to MAX_OUTPUT_LEN, inserting an ellipsis in the middle."""
    if len(text) <= MAX_OUTPUT_LEN:
        return text
    half = MAX_OUTPUT_LEN // 2
    return text[:half] + "\n\n... [truncated] ...\n\n" + text[-half:]


def format_result(result: dict, elapsed: float) -> str:
    """Render a worker result dict as a human-readable string."""
    status = "Success" if result["ok"] else "Failed"
    parts  = [f"{status} ({elapsed}s)"]
    if result.get("stdout"):
        parts += ["\nstdout:", truncate(result["stdout"])]
    if result.get("stderr"):
        parts += ["\nstderr:", truncate(result["stderr"])]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Persistent worker
# ---------------------------------------------------------------------------

class PersistentWorker:
    """
    Manages a single long-running ``worker.py`` subprocess.

    Code is sent as a newline-delimited JSON request over stdin; results
    arrive as a newline-delimited JSON response on stdout. This eliminates
    the per-call interpreter startup cost (~200–500 ms) of spawning a fresh
    process each time.

    A per-call *timeout* is enforced via :func:`asyncio.wait_for`; if the
    worker exceeds it the process is killed and restarted transparently.
    An :class:`asyncio.Lock` serialises concurrent calls so requests and
    responses are never interleaved on the pipe.
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            PYTHON_BIN, WORKER_SCRIPT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _ensure_alive(self) -> None:
        if self._proc is None or self._proc.returncode is not None:
            await self._start()

    async def _kill(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                await self._proc.wait()
            except ProcessLookupError:
                pass
            self._proc = None

    # ------------------------------------------------------------------
    # Communication
    # ------------------------------------------------------------------

    async def _send(self, payload: dict) -> None:
        """Write a JSON line to the worker's stdin."""
        self._proc.stdin.write((json.dumps(payload) + "\n").encode())
        await self._proc.stdin.drain()

    async def _recv(self, timeout: int) -> dict:
        """Read a JSON line from the worker's stdout, respecting *timeout*."""
        try:
            raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._kill()
            return _error(f"Timed out after {timeout}s.")
        except Exception as exc:  # noqa: BLE001
            await self._kill()
            return _error(f"Worker error: {exc}")

        if not raw:
            await self._kill()
            return _error("Worker exited unexpectedly.")

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            return _error(f"Bad worker response: {exc}")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self, code: str, timeout: int, env_vars: dict) -> dict:
        """
        Send *code* to the worker and return a result dict
        ``{"ok": bool, "stdout": str, "stderr": str}``.
        """
        async with self._lock:
            await self._ensure_alive()

            payload = {"code": code, "env_vars": env_vars}
            try:
                await self._send(payload)
            except (BrokenPipeError, ConnectionResetError):
                # Worker died between calls — restart and retry once.
                await self._kill()
                await self._start()
                await self._send(payload)

            return await self._recv(timeout)


def _error(message: str) -> dict:
    return {"ok": False, "stdout": "", "stderr": message}


# ---------------------------------------------------------------------------
# pip installer
# ---------------------------------------------------------------------------

def pip_install(packages: List[str], upgrade: bool = False) -> dict:
    """
    Run ``pip install`` and return a structured result dict.

    Returns keys: ``success`` (bool), ``elapsed_s`` (float),
    ``stdout`` (str), ``stderr`` (str), ``timed_out`` (bool).
    """
    cmd = [PYTHON_BIN, "-m", "pip", "install", "--quiet"]
    if upgrade:
        cmd.append("--upgrade")
    cmd += packages

    try:
        t0    = time.perf_counter()
        proc  = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        elapsed = round(time.perf_counter() - t0, 3)
        return {
            "success":   proc.returncode == 0,
            "elapsed_s": elapsed,
            "stdout":    truncate(proc.stdout),
            "stderr":    truncate(proc.stderr),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "elapsed_s": 180, "stdout": "", "stderr": "", "timed_out": True}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "elapsed_s": 0, "stdout": "", "stderr": str(exc), "timed_out": False}