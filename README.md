# clangd-mcp

> **Experimental** — expect rough edges and breaking changes.

**Deprecation notice**: This project is unmaintained.

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
- [uv](https://docs.astral.sh/uv/)
- [clangd](https://clangd.llvm.org/installation) on `$PATH` (or specify `--clangd`)
- A `compile_commands.json` for your project (CMake: `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`)

## Installation

```bash
uv tool install git+https://github.com/schuay/clangd-mcp.git
```

This installs a `clangd-mcp` command into an isolated environment and puts a
shim on your `$PATH`.  Upgrade later with:

```bash
uv tool upgrade clangd-mcp
```

### Gemini CLI

```bash
curl -fsSL https://raw.githubusercontent.com/schuay/clangd-mcp/main/install-gemini.sh | bash
```

This installs the tool, prompts for project paths, and adds the server to
`~/.gemini/settings.json`. Requires `jq`.

Or add manually to `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "clangd": {
      "command": "clangd-mcp",
      "args": [
        "--compile-commands-dir", "/path/to/your/build",
        "--workspace-dir", "/path/to/your/project",
        "--seed-file", "/path/to/your/project/src/main.cpp"
      ]
    }
  }
}
```

## Options

All flags are optional:

| Flag | Default | Description |
|---|---|---|
| `--clangd` | `clangd` | Path to the clangd binary |
| `--compile-commands-dir` | *(none)* | Directory containing `compile_commands.json` |
| `--workspace-dir` | current directory | Root of the C/C++ project |
| `--seed-file` | *(from compile_commands.json)* | Source file to open at startup (gives clangd a first indexing target). Optional: the server picks the first workspace source listed in `compile_commands.json` |
| `--index-timeout-seconds` | `600` | Maximum time to wait for a started indexing pass to finish |
| `--index-wait-seconds` | `10` | Maximum time a tool call blocks waiting for the index before answering with a "still indexing" notice |
| `--log-level` | `WARNING` | `DEBUG` / `INFO` / `WARNING` / `ERROR` (to stderr) |

The server starts immediately and never blocks on indexing. Index-dependent
tools only wait while clangd is actually indexing (finishing within a second on
a warm index), and while it is still indexing they say so instead of reporting
"No symbols found".

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
pyproject.toml Dependencies (mcp>=2.0,<3)
```
