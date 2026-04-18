# python_runner_mcp

An MCP server that lets Claude execute Python code and manage packages in the active Python environment.

---

## Tools

### `python_run`

Execute Python code in an isolated subprocess.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `code` | `str` | required | Python source code to execute |
| `timeout` | `int` | `30` | Max execution time in seconds (1–120) |
| `env_vars` | `dict` | `{}` | Extra environment variables to pass to the subprocess |

Returns plain text with the status, exit code, elapsed time, stdout, and stderr.

---

### `install_packages`

Install pip packages into the active Python environment. Installed packages are immediately importable in subsequent `python_run` calls.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `packages` | `List[str]` | required | pip specifiers, e.g. `["numpy", "pandas>=2", "requests==2.31"]` |
| `upgrade` | `bool` | `false` | Pass `--upgrade` to reinstall or upgrade already-installed packages |

Returns a single line:
- **Success**: `installed: numpy, pandas (3.2s)`
- **Failure**: `install failed: <pip error>`
- **Timeout**: `install timed out after 180s.`

---

## Setup

```bash
# 1. Create & activate a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Verify the server starts
python server.py
# No output is normal — the server is listening on stdio.
```

---

## Claude Desktop integration

Add the following block to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "python_runner": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"]
    }
  }
}
```

Replace the paths with the actual absolute paths on your machine.

**Config file locations:**
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

Restart Claude Desktop after editing.

---

## Security note

Both tools execute code or install packages with the same privileges as the server process. Only connect this server to trusted Claude Desktop sessions and do **not** expose it over the network.

### OS operation blocking

Before any code is executed, `python_run` parses it as an AST and rejects it if it contains:

- **Blocked imports** — `os`, `sys`, `subprocess`, `shutil`, `pathlib`, `socket`, `ctypes`, `multiprocessing`, `tempfile`, `glob`, `signal`, `importlib`, `builtins`, and other OS-level modules.
- **Blocked builtins** — `open`, `exec`, `eval`, `compile`, `__import__`, `breakpoint`.

Blocked code is never executed and returns a `Blocked: ...` error message instead. Note that this is AST-level static analysis, not a full sandbox — it is a strong first line of defence but should not be considered a substitute for OS-level isolation.