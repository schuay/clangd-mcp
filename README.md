# mcp-clangd

A minimal [Model Context Protocol](https://modelcontextprotocol.io/) server that
bridges AI assistants to [clangd](https://clangd.llvm.org/) for C/C++ code
intelligence.

## How it works

```
Claude / Gemini  ←─ MCP (stdio) ─→  main.py  ←─ LSP (stdio) ─→  clangd
```

`main.py` speaks MCP to the AI client and LSP (JSON-RPC 2.0 over stdin/stdout)
to clangd. The two protocols are bridged by three tools:

| Tool | LSP call | Description |
|---|---|---|
| `find_symbol` | `workspace/symbol` | Search symbols by name (fuzzy) |
| `get_definition` | `textDocument/definition` | Show the definition site with source |
| `find_references` | `textDocument/references` | List every usage, grouped by file |

## Requirements

- Python ≥ 3.12
- [uv](https://docs.astral.sh/uv/) (or any other Python package manager)
- [clangd](https://clangd.llvm.org/installation) on `$PATH` (or specify `--clangd`)
- A `compile_commands.json` for your project (CMake: `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`)

## Installation

```bash
git clone https://github.com/youruser/mcp-clangd
cd mcp-clangd
uv sync          # creates .venv and installs mcp
```

## Running manually (for testing)

```bash
uv run python main.py \
  --clangd /usr/bin/clangd \
  --compile-commands-dir /path/to/build \
  --workspace-dir /path/to/project \
  --log-level INFO
```

All flags are optional:

| Flag | Default | Description |
|---|---|---|
| `--clangd` | `clangd` | Path to the clangd binary |
| `--compile-commands-dir` | *(none)* | Directory containing `compile_commands.json` |
| `--workspace-dir` | current directory | Root of the C/C++ project |
| `--log-level` | `WARNING` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (to stderr) |

## Configure with Claude Desktop

Add to `~/.config/Claude/claude_desktop_config.json` (Linux) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "clangd": {
      "command": "/absolute/path/to/mcp-clangd/.venv/bin/python",
      "args": [
        "/absolute/path/to/mcp-clangd/main.py",
        "--compile-commands-dir", "/path/to/your/build",
        "--workspace-dir", "/path/to/your/project"
      ]
    }
  }
}
```

## Configure with Gemini CLI

Gemini CLI supports MCP servers via
[fastmcp](https://github.com/jlowin/fastmcp) as the runner.  Install fastmcp
once:

```bash
uv tool install fastmcp
```

Then add to `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "clangd": {
      "command": "fastmcp",
      "args": [
        "run",
        "/absolute/path/to/mcp-clangd/main.py",
        "--",
        "--compile-commands-dir", "/path/to/your/build",
        "--workspace-dir", "/path/to/your/project"
      ]
    }
  }
}
```

`fastmcp run` takes care of activating the right virtualenv and passing
arguments after `--` through to the server.

> **Note:** if your project's `.venv` already has all dependencies, you can
> also point directly at the venv Python:
> ```json
> "command": "/absolute/path/to/mcp-clangd/.venv/bin/python",
> "args": ["main.py", "--compile-commands-dir", "..."]
> ```

## Tests

```bash
uv run python tests.py -v
```

The test suite runs without a real clangd binary — it drives the LSP client
with canned in-process responses and patches the global `lsp` object when
testing the MCP tool handlers.

## File structure

```
main.py        MCP server: tools, arg parsing, clangd lifecycle
lsp_client.py  LSP client: subprocess management, JSON-RPC framing, queries
tests.py       Unit tests (no clangd required)
pyproject.toml Dependencies (mcp>=1.0)
```
