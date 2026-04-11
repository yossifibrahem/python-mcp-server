# python_runner_mcp

An MCP server that exposes a single tool — **`run_python`** — which executes arbitrary Python code in a sandboxed subprocess and returns stdout, stderr, and the exit code.

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
# You should see the FastMCP server listening on stdio (no output is normal).
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

## Tool reference

### `run_python`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `code` | `str` | required | Python source code to execute |
| `timeout` | `int` | `30` | Max execution time in seconds (1–120) |
| `stdin` | `str` | `null` | Optional data to pipe into the script |

**Returns** (JSON):

```json
{
  "stdout": "Hello, world!\n",
  "stderr": "",
  "exit_code": 0,
  "timed_out": false,
  "error": null
}
```

---

## Security note

`run_python` executes **arbitrary code** with the same privileges as the server process. Only connect this server to trusted Claude Desktop sessions and do **not** expose it over the network.
