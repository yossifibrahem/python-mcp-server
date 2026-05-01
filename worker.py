"""
Persistent worker process for python_runner_mcp.

Reads newline-delimited JSON requests from stdin, executes the code,
and writes newline-delimited JSON responses to stdout.

Request:  {"code": "...", "env_vars": {"KEY": "VAL"}}
Response: {"ok": true/false, "stdout": "...", "stderr": "..."}
"""

import json
import sys
import io
import os
import contextlib
import traceback

# Each call gets its own fresh namespace — isolated like the original subprocess approach.
# Swap this for a single module-level dict if you want REPL-style persistent state.
def _exec_isolated(code: str) -> dict:
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            exec(compile(code, "<string>", "exec"), {})  # noqa: S102
        return {"ok": True, "stdout": stdout_buf.getvalue(), "stderr": stderr_buf.getvalue()}
    except SystemExit as exc:
        return {
            "ok": exc.code in (0, None),
            "stdout": stdout_buf.getvalue(),
            "stderr": f"SystemExit({exc.code})",
        }
    except Exception:  # noqa: BLE001
        return {"ok": False, "stdout": stdout_buf.getvalue(), "stderr": traceback.format_exc()}


def main() -> None:
    # Use raw binary stdin so readline() never blocks on buffering issues
    stdin = open(sys.stdin.fileno(), "rb", buffering=0)  # noqa: WPS515

    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue

        # --- parse request ------------------------------------------------
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            resp = {"ok": False, "stdout": "", "stderr": f"worker: bad JSON — {exc}"}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue

        code = req.get("code", "")
        env_vars: dict = req.get("env_vars") or {}

        # --- apply / restore extra env vars --------------------------------
        old_env: dict = {}
        for key, val in env_vars.items():
            old_env[key] = os.environ.get(key)
            os.environ[key] = str(val)

        # --- execute -------------------------------------------------------
        result = _exec_isolated(code)

        # --- restore env ---------------------------------------------------
        for key, old_val in old_env.items():
            if old_val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_val

        # --- respond -------------------------------------------------------
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()