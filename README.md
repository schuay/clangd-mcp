# mcp-clangd

> **Experimental** — expect rough edges and breaking changes.

A minimal [Model Context Protocol](https://modelcontextprotocol.io/) server that
bridges AI assistants to [clangd](https://clangd.llvm.org/) for C/C++ code
intelligence.

## How it works

```
Claude / Gemini  ←─ MCP (stdio) ─→  server.py  ←─ LSP (stdio) ─→  clangd
```

`server.py` speaks MCP to the AI client and LSP (JSON-RPC 2.0 over stdin/stdout)
to clangd. The two protocols are bridged by nine tools:

| Tool | LSP call(s) | Description |
|---|---|---|
| `find_symbol` | `workspace/symbol` | Search symbols by name (fuzzy) |
| `get_definition` | `workspace/symbol` → `textDocument/definition` | Show the definition site with source |
| `find_references` | `workspace/symbol` → `textDocument/references` | List every usage, grouped by file |
| `get_type_info` | `workspace/symbol` → `textDocument/hover` | Show type signature and doc comment |
| `find_implementations` | `workspace/symbol` → `textDocument/implementation` | Find concrete implementations of a virtual method or interface |
| `get_callers` | `workspace/symbol` → `prepareCallHierarchy` → `incomingCalls` | Find every call site that calls a function |
| `get_callees` | `workspace/symbol` → `prepareCallHierarchy` → `outgoingCalls` | Find every function called by a function |
| `list_file_symbols` | `textDocument/documentSymbol` | List all symbols defined in a file |
| `get_type_hierarchy` | `workspace/symbol` → `prepareTypeHierarchy` → `supertypes` + `subtypes` | Show base classes and derived classes |

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
uv run python server.py \
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
        "/absolute/path/to/mcp-clangd/server.py",
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
        "/absolute/path/to/mcp-clangd/server.py",
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
> "args": ["server.py", "--compile-commands-dir", "..."]
> ```

## Tests

```bash
uv run python tests.py
# or with pytest for coloured output:
uv run pytest tests.py -v
```

The test suite runs without a real clangd binary — it drives the LSP client
with canned in-process responses and patches the global `lsp` object when
testing the MCP tool handlers.

## File structure

```
server.py        MCP server: tools, arg parsing, clangd lifecycle
lsp_client.py  LSP client: subprocess management, JSON-RPC framing, queries
tests.py       Unit tests (no clangd required)
pyproject.toml Dependencies (mcp>=1.0)
```
