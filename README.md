# python_runner_mcp

An MCP server that lets Claude execute Python code and manage packages in the active Python environment.

---

## Tools

### `python_run`

Execute Python code in a persistent worker subprocess.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `code` | `str` | required | Python source code to execute |
| `timeout` | `int` | `30` | Max execution time in seconds (1–120) |
| `env_vars` | `dict` | `{}` | Extra environment variables to pass to the subprocess |

Returns plain text with the status, elapsed time, stdout, and stderr.

---

### `pip_install`

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

## Configuration

### `ALLOW_MODULES`

By default, a set of OS-level modules is blocked from being imported in executed code (see [Security](#security) below). You can selectively unblock modules by setting the `ALLOW_MODULES` environment variable in your MCP config:

```json
{
  "mcpServers": {
    "python_runner": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"],
      "env": {
        "ALLOW_MODULES": "os,pathlib"
      }
    }
  }
}
```

`ALLOW_MODULES` accepts a comma-separated list of module names. Unblocking a top-level module also unblocks its sub-modules — allowing `os` implicitly allows `os.path` as well.

When any modules are allowed, the effective allow and block lists are logged to stderr on startup so you can confirm the config was picked up after restarting Claude Desktop.

---

## Architecture

`python_run` calls are handled by a **persistent worker subprocess** (`worker.py`) that stays alive for the lifetime of the server. Code is sent to it as a JSON line over stdin, and results are returned as a JSON line over stdout — eliminating the interpreter startup cost (~200–500ms) that would otherwise be paid on every single call.

If the worker hangs (e.g. infinite loop) or crashes, it is killed and restarted automatically. Concurrent calls are serialised via an asyncio lock so requests and responses are never interleaved on the pipe.

`pip_install` still uses a fresh subprocess per call, which is appropriate since pip itself is the slow part.

---

## Security

Before any code reaches the worker, `python_run` parses it as an AST and rejects it if it contains:

**Blocked imports** (by default):

`os`, `os.path`, `sys`, `subprocess`, `shutil`, `pathlib`, `socket`, `socketserver`, `ctypes`, `ctypes.util`, `multiprocessing`, `concurrent.futures`, `tempfile`, `glob`, `fnmatch`, `signal`, `resource`, `mmap`, `pwd`, `grp`, `fcntl`, `termios`, `tty`, `pty`, `winreg`, `winsound`, `msvcrt`, `importlib`, `importlib.util`, `importlib.machinery`, `builtins`, `gc`, `inspect`, `dis`

**Blocked builtins** (always, not configurable):

`open`, `exec`, `eval`, `compile`, `__import__`, `breakpoint`

Blocked code is never executed and returns a `Blocked: ...` error message instead. The builtins list is not affected by `ALLOW_MODULES` — these are the most direct escape hatches and should only be changed in code, not config.

Note that this is AST-level static analysis, not a full sandbox — it is a strong first line of defence but should not be considered a substitute for OS-level isolation. Only connect this server to trusted Claude Desktop sessions and do **not** expose it over the network.