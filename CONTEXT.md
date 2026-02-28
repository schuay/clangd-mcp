# Development Context for mcp-clangd

This document captures the full design rationale, protocol mechanics, and architectural decisions made during initial development. It is written so that a new AI agent (or human) can continue work without re-deriving any of it.

---

## What this project does

It bridges two protocols:

```
AI client (Claude / Gemini)
        ↕  MCP over stdio
    main.py  (this server)
        ↕  LSP over stdio
      clangd  (subprocess)
```

The AI client invokes MCP tools. Each tool translates user intent into one or more LSP requests to clangd, formats the results as plain text, and returns them.

---

## Protocol 1: Model Context Protocol (MCP)

MCP is the protocol used between AI assistants and tool servers. The Python library is `mcp` (PyPI), specifically `mcp.server.fastmcp.FastMCP`.

**How FastMCP works:**
- `FastMCP("name", lifespan=ctx)` creates a server.
- `@mcp.tool()` decorators on async functions register tools. The function's docstring becomes the tool description shown to the AI; the parameter names and type annotations become the tool's input schema.
- `mcp.run()` starts the stdio transport. Internally it uses `anyio` (which adapts to asyncio) and calls into `mcp.server.stdio.stdio_server()`.
- The `lifespan` parameter is an `@asynccontextmanager` async generator. It runs setup code before `yield` and teardown after. This is where the LSP client is started and stopped. The lifespan runs inside the same asyncio event loop as the tool handlers, so all `await` calls in tools and lifespan share the same loop — this is what makes the async LSP client work without a separate thread.

**Important:** Args must be parsed at module import time (before `mcp.run()` starts its own loop), because `mcp.run()` calls `anyio.run()` which creates the event loop and then immediately runs the lifespan. There's no hook to inject pre-loop work. The pattern used here is calling `_parse_args()` at module level so `args` is available when the lifespan closure runs.

**Tool return values:** Tools return plain strings. FastMCP wraps them in the MCP `TextContent` response format automatically.

---

## Protocol 2: Language Server Protocol (LSP)

LSP is a JSON-RPC 2.0 protocol, defined at https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/

### Message framing

Every message (in both directions) is framed with HTTP-style headers:

```
Content-Length: 123\r\n
\r\n
{"jsonrpc":"2.0","id":1,"method":"initialize",...}
```

The only header that matters in practice is `Content-Length`. The body is always UTF-8 JSON. There is no Content-Type header required by clangd (it ignores it). The `_read_message()` method reads lines until it sees a blank line, extracts `Content-Length`, then does `readexactly(content_length)` to get the body atomically.

### Message types

**Request** (client→server, expects response):
```json
{"jsonrpc": "2.0", "id": 1, "method": "workspace/symbol", "params": {"query": "foo"}}
```

**Response** (server→client, matches a request by id):
```json
{"jsonrpc": "2.0", "id": 1, "result": [...]}
{"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "..."}}
```

**Notification** (either direction, no id, no response expected):
```json
{"jsonrpc": "2.0", "method": "initialized", "params": {}}
```

clangd also sends server-initiated requests (e.g. `window/showMessage`, `textDocument/publishDiagnostics`) and server-initiated requests-with-id (e.g. `workspace/applyEdit`). The current implementation silently ignores all of these in `_dispatch()` — they fall through the `if msg_id is not None and msg_id in self._pending` check without matching anything. This is fine for read-only queries but must be handled if implementing write operations like rename.

### Request/response correlation

`LSPClient._pending` is a `dict[int, asyncio.Future]`. When `request()` is called:
1. Increment `_next_id`, store it as `req_id`.
2. Create a `Future` on the running loop and store it: `_pending[req_id] = future`.
3. Send the message.
4. `await asyncio.wait_for(future, timeout=30.0)`.

When `_read_loop()` receives a message with a matching `id`, it calls `future.set_result(msg["result"])` (or `set_exception` on error), which unblocks the awaiting coroutine.

The 30-second timeout on every request prevents hangs if clangd crashes or stalls.

### Initialization handshake

LSP requires an exact two-step handshake before any other requests:

1. **Client sends `initialize` request** with:
   - `processId`: our PID (lets the server detect if client crashes)
   - `rootUri`: `file:///absolute/path/to/workspace` (tells clangd where to look)
   - `workspaceFolders`: same URI as a list (belt-and-suspenders; some servers prefer this)
   - `capabilities`: what LSP features the client supports. We declare only what we actually use. Declaring `workspace.symbol.symbolKind.valueSet: [1..26]` tells clangd it can return all symbol kinds; without this it may filter results.

2. **Server responds** with its own capabilities and `serverInfo`.

3. **Client sends `initialized` notification** (no params, empty `{}`). This is required — clangd won't respond to queries until it receives it.

clangd starts background indexing after receiving `initialized`. Queries made before indexing completes will return partial results. The current code does not wait for indexing to finish, which is acceptable for interactive use — results improve as clangd indexes.

### File lifecycle: why `textDocument/didOpen` is required

clangd's `textDocument/definition` and `textDocument/references` operate on the **document model**, not the filesystem. A file must be "opened" (registered with the document model) before position-based queries on it will work. `workspace/symbol` is an exception — it searches the index and does not require open files.

`textDocument/didOpen` sends:
```json
{
  "textDocument": {
    "uri": "file:///path/to/foo.cpp",
    "languageId": "cpp",
    "version": 1,
    "text": "<full file contents>"
  }
}
```

The file version number starts at 1 and must be incremented with each `textDocument/didChange`. Since we never send `didChange`, it stays at 1 permanently. `_open_files` is a set of URIs that have been opened, used to prevent sending duplicate `didOpen` notifications (which would be an LSP error).

`textDocument/didClose` is not sent on shutdown in the current implementation. This is benign — clangd cleans up when it receives the `exit` notification.

### The three LSP queries used

**`workspace/symbol`**
- Params: `{"query": "string"}`
- Returns: `SymbolInformation[]` (LSP 3.16) or `WorkspaceSymbol[]` (LSP 3.17)
- Both have `name`, `kind` (int 1–26), `containerName` (parent scope), and `location` (`{uri, range}`)
- clangd performs fuzzy matching on the query string. An empty query returns all symbols (potentially thousands). The results are not ranked by relevance in any guaranteed order.
- Response is capped to 50 in the tool output to avoid overwhelming the AI context window. This cap is only on display — the full list is returned by clangd.

**`textDocument/definition`**
- Params: `{"textDocument": {"uri": "..."}, "position": {"line": N, "character": N}}`
- Line and character are **0-based**. This is a common gotcha — display to users uses 1-based.
- Returns: `Location | Location[] | LocationLink[]`. clangd returns `Location[]` in practice, but can return a single `Location` (non-list). `definition()` normalizes this by always returning `list[dict]`.
- The key use case: a symbol declared in a `.h` header will have its workspace/symbol location pointing to the declaration. `textDocument/definition` from that location returns the definition in the `.cpp` file.

**`textDocument/references`**
- Params: `{"textDocument": ..., "position": ..., "context": {"includeDeclaration": true}}`
- Returns: `Location[]`
- `includeDeclaration: true` means the declaration site is included in the results. Without it you'd miss the header declaration.
- Results are across all files clangd has indexed, not just open files.

---

## Architecture walkthrough: what happens when a tool is called

### `find_symbol("MyClass")`

1. FastMCP calls `find_symbol("MyClass")` in the asyncio event loop.
2. `lsp.workspace_symbol("MyClass")` calls `lsp.request("workspace/symbol", {"query": "MyClass"})`.
3. `request()` allocates a Future, stores it in `_pending[N]`, serializes and writes the JSON-RPC request to clangd's stdin.
4. `_read_loop()` (a background task in the same event loop) reads clangd's response from stdout, calls `_dispatch()`, which resolves `_pending[N]`.
5. `request()` returns the result (list of SymbolInformation dicts).
6. The tool formats them as a string and returns it.

### `get_definition("MyClass")`

1. Call `workspace/symbol` to get the declaration location (same as above).
2. Pick the exact name match if one exists, else first result.
3. Call `open_file(decl_path)` → sends `textDocument/didOpen` if not already open.
4. Call `textDocument/definition` at the declaration position.
5. clangd resolves this to the definition location (possibly in a different file).
6. Read that file from disk, extract ±4 lines of context around the definition line, format with `>>>` marker.

The fallback: if `textDocument/definition` returns nothing (e.g. the symbol is defined inline in the header, so declaration = definition, and clangd returns empty), the tool falls back to displaying context at the `workspace/symbol` location.

### `find_references("MyClass")`

1–3: Same as `get_definition` up through `open_file`.
4. Call `textDocument/references` with `includeDeclaration: true`.
5. Group resulting `Location[]` by file path.
6. For each file, sort refs by line number, read the source line, display as `line:col  source_text`.

---

## Key design decisions

**Python + asyncio, not Go.** The reference implementation (isaacphi/mcp-language-server) is in Go with goroutines. Python with asyncio is equally capable here: both protocols are line-oriented I/O, and asyncio handles the concurrency cleanly with tasks. The resulting code is shorter and has no compile step. The only performance concern would be high-frequency queries (e.g. hover on every keystroke in an editor), which is not the MCP use case.

**No LSP library.** There are Python LSP client libraries (pygls, lsp-client), but they add abstraction that obscures what's happening. The protocol is simple enough to implement directly: 150 lines of transport code covers everything needed. Adding a library would make the code harder to read and debug without adding capability.

**Flat file structure.** Two `.py` files instead of a package. The project is small and focused. A `src/mcp_clangd/` package layout would add indirection with no benefit.

**Symbol-name-based tool API.** The three tools all accept a symbol name string rather than a file path + line + column. This matches how an AI assistant naturally thinks about code ("show me the definition of `MyClass`" not "go to /path/foo.h line 42 col 8"). The position-based LSP API is used internally, resolved via `workspace/symbol` first.

**No wait after `didOpen`.** Some LSP clients sleep 0.5–1s after opening a file to let the server parse it before querying. We skip this because: (a) it would add latency to every query, (b) clangd handles `textDocument/definition` synchronously from its AST even before diagnostics are ready, (c) it would make testing harder. If clangd returns empty results for definition on a freshly opened file, the fallback to the workspace/symbol location handles it gracefully.

**Server-initiated messages silently ignored.** clangd sends `window/logMessage`, `textDocument/publishDiagnostics`, `$/progress` (indexing status) and others. Ignoring them is correct for read-only tools. If diagnostics or code actions are added, `_dispatch` needs to be extended with notification handler registration.

**`--log=error` passed to clangd.** Without this, clangd emits verbose progress messages to stderr. We suppress them to keep the MCP server's stderr (which goes to the AI client's log) clean. Set `--log-level DEBUG` on the MCP server and `--log=verbose` on clangd to see the full wire traffic during debugging.

---

## Source reference: isaacphi/mcp-language-server

This Go implementation was studied as the primary reference. Key things learned from it:

- The two-task pattern: one goroutine (task) for reading, one for writing. Reading happens exclusively in `_read_loop`; writing happens in `request()`/`notify()`. This avoids races on the pipe.
- The channel (Future) map pattern for correlating requests to responses by integer ID.
- The initialization sequence: `initialize` → wait for response → `initialized` notification → register handlers.
- That `textDocument/didOpen` requires the full file text in the notification body.
- That `workspace/symbol` does not require open files.
- Graceful shutdown: `shutdown` request → `exit` notification → terminate/kill subprocess.
- Parent process monitoring (the reference monitors for orphaning when Claude Desktop closes). Not implemented here — MCP servers are expected to exit when their stdio is closed, which happens automatically when FastMCP's event loop exits.

The reference uses `gopls`, `typescript-language-server`, and others; the same LSP wire protocol works identically for clangd.

---

## Known limitations and edge cases

**`workspace/symbol` fuzzy matching is clangd's**: clangd's matching is good but not perfect. `find_symbol("foo")` may return `fooBar`, `FooBase`, etc. The exact-name-match preference in `get_definition` and `find_references` partially mitigates this, but homonyms (same name in different namespaces) are not handled — the first exact match wins.

**`textDocument/definition` from a declaration may return the declaration itself.** If the function is defined in the header (inline, template, etc.), `definition` returns a location pointing back to the same place. This is correct behavior, not a bug. The tool displays it correctly.

**Indexing latency.** clangd builds its index in the background after `initialized`. `workspace/symbol` on a large codebase will return partial results for the first several seconds. There is no way in the current protocol to wait for indexing to complete without polling `$/progress` notifications, which are currently ignored.

**`uri_to_path` is naive.** It strips the `file://` prefix by slicing at index 7. This is correct for absolute Unix paths (`file:///home/...` → `/home/...` since `///` → `/`). Windows paths (`file:///C:/...`) and URIs with percent-encoded characters would need `urllib.parse.unquote(urllib.request.url2pathname(...))`. This project targets Linux/macOS.

**No `textDocument/didClose`.** Open files accumulate in `_open_files` for the lifetime of the server. For typical MCP sessions (minutes to hours) this is not a problem. For long-running sessions against huge projects, clangd could accumulate stale document state. Adding `close_file()` and calling it from tools would fix this.

**Request timeout is 30 seconds.** This is generous for interactive queries. `workspace/symbol` on an unindexed 10M-line codebase might time out. The timeout can be increased if needed.

**No concurrent request safety.** `_next_id` is a plain integer incremented in `request()`. Since Python's asyncio is single-threaded and `request()` is a coroutine (not called from threads), there is no race. If threads were introduced, this would need `threading.Lock` or `asyncio.Lock`.

---

## How to add a new tool

1. Add a method to `LSPClient` in `lsp_client.py` that calls `self.request(method, params)` with the appropriate LSP method name and parameters. Return the raw result or `[]`/`{}` on null.

2. Add an `@mcp.tool()` async function in `main.py`. Follow the pattern:
   - Assert `lsp is not None`.
   - Call `lsp.workspace_symbol(name)` to locate the symbol if the tool takes a symbol name.
   - Call `lsp.open_file(path)` before any position-based query.
   - Format the result as a human-readable string.

3. Add tests in `tests.py`:
   - A `TestLSPFeatureMethods` test for the `LSPClient` method (mock `request`).
   - A `TestMCPTools` test for the tool function (mock the global `lsp`).

**LSP methods worth adding:**
- `textDocument/hover` → type info and documentation at a position
- `textDocument/documentSymbol` → all symbols in a specific file
- `textDocument/diagnostics` or cached `publishDiagnostics` → compile errors/warnings
- `textDocument/rename` → rename a symbol across the workspace (requires handling `workspace/applyEdit` server request)
- `workspace/executeCommand` → clangd-specific commands like `clangd.applyFix`

For `hover` and `documentSymbol`, the tool API would naturally take `(file_path, line, col)` rather than a symbol name, since the user already has a file open.

For `rename`, the server will send a `workspace/applyEdit` request back to the client. This requires extending `_dispatch()` to handle server-initiated requests, sending back a response, and applying the edits to disk.

---

## Testing approach

Tests in `tests.py` use Python's `unittest.IsolatedAsyncioTestCase` which creates a fresh asyncio event loop per test method. No pytest is needed (but `uv run pytest tests.py -v` also works).

**Four layers tested:**

1. **URI helpers** — pure functions, sync `TestCase`.
2. **Message framing** — `TestMessageFraming` injects a real `asyncio.StreamReader` pre-loaded with hand-crafted LSP frames and drives `_read_message()` / `_send()` directly. The process object is a `MagicMock` with the stream attached to `proc.stdout`.
3. **Request/response correlation** — `TestRequestResponse` replaces `_send` with a fake that immediately resolves the pending future, testing `request()`, `notify()`, and `_dispatch()` without network I/O.
4. **LSP feature methods** — `TestLSPFeatureMethods` replaces `request` and `notify` on the client with coroutines returning canned responses, testing `workspace_symbol`, `definition`, `references`, `open_file`.
5. **MCP tool handlers** — `TestMCPTools` uses `unittest.mock.patch.object(main, "lsp", mock_lsp)` to inject an `AsyncMock(spec=LSPClient)` as the global `lsp` in `main.py`, then calls the tool coroutines directly.

The test for `open_file` creates a real temporary file because `open_file()` reads the file from disk to get its text content for the `didOpen` notification.

---

## Dependencies

| Package | Purpose |
|---|---|
| `mcp>=1.0.0` | FastMCP server, MCP stdio transport |
| `anyio` | Async runtime (transitive dep of mcp; compatible with asyncio) |
| `pydantic` | Transitive dep of mcp; not used directly |

Everything else in the lockfile is transitive. The `mcp` package brings in `httpx`, `starlette`, `uvicorn` etc. for its HTTP/SSE transport — these are unused in stdio mode but are installed regardless.

---

## Running and configuration

```bash
# Development
uv run python main.py --workspace-dir /path/to/project --log-level DEBUG

# Tests
uv run python tests.py

# MCP client config (Claude Desktop, Gemini CLI, etc.)
# command: /path/to/.venv/bin/python
# args: ["/path/to/main.py", "--compile-commands-dir", "/path/to/build"]
```

clangd discovers `compile_commands.json` by searching upward from each source file. If the build directory is not a parent of the source tree, `--compile-commands-dir` is required. Without it, clangd falls back to a default compilation database with no include paths, which means it cannot resolve cross-file symbols correctly and `definition` / `references` will give poor results.
