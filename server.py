# Copyright 2026 The clangd-mcp developers
# SPDX-License-Identifier: MIT

"""MCP server that provides rich C/C++ language intelligence to LLMs via clangd.

This server enables LLMs to deeply understand, navigate, and analyze C/C++ codebases by
bridging the Model Context Protocol (MCP) to the Language Server Protocol (LSP).

As a client, you are encouraged to use these tools aggressively to explore the
codebase, resolve symbols, and understand complex relationships (inheritance, call
graphs, etc.) before proposing changes.

Note the server may be upgraded via: uv tool upgrade clangd-mcp
"""

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import pathlib
import sys
from contextlib import asynccontextmanager
from typing import Literal

from mcp.server.mcpserver import MCPServer

from lsp_client import LSPClient, SYMBOL_KINDS, uri_to_path

# ---------------------------------------------------------------------------
# Argument parsing (done at import time so args are available in lifespan)
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="MCP server bridging Claude to clangd",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--clangd", default="clangd", metavar="PATH",
                   help="Path to the clangd binary")
    p.add_argument("--compile-commands-dir", metavar="DIR",
                   help="Directory containing compile_commands.json")
    p.add_argument("--workspace-dir", metavar="DIR", default=os.getcwd(),
                   help="Root directory of the C/C++ project")
    p.add_argument("--seed-file", metavar="FILE",
                   help="Source file to open at startup to trigger clangd background indexing "
                        "(optional; auto-detected from compile_commands.json)")
    p.add_argument("--index-timeout-seconds", type=float, default=600.0, metavar="SEC",
                   help="Maximum time to wait for a started indexing cycle to finish")
    p.add_argument("--index-wait-seconds", type=float, default=10.0, metavar="SEC",
                   help="Maximum time an index-dependent tool call blocks waiting for the "
                        "index before answering with a 'still indexing' notice")
    p.add_argument("--log-level", default="WARNING",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Logging verbosity (goes to stderr)")
    return p.parse_args()



args = _parse_args()

logging.basicConfig(
    level=getattr(logging, args.log_level),
    stream=sys.stderr,
    format="%(levelname)s %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# LSP client — created during lifespan, used by tool handlers
# ---------------------------------------------------------------------------

logger = logging.getLogger("server")

lsp: LSPClient | None = None


# ---------------------------------------------------------------------------
# Server-initiated request relay — clangd -> MCP client
# ---------------------------------------------------------------------------
#
# clangd sends window/workDoneProgress/create and window/showMessageRequest as
# *requests*, i.e. it expects a reply.  The MCP SDK answers any request method
# it does not implement with a METHOD_NOT_FOUND error, and clangd treats that
# as "no support" and silently disables the feature (this is why background
# index progress never arrived before).  The read-stream proxy below forwards
# those two methods to the MCP client, surfaces the client's answer as an LSP
# response, and passes every other message through untouched.

try:  # SDK plumbing; import-guarded in case of version drift
    from mcp.shared.message import SessionMessage as _SessionMessage
    from mcp_types import (
        JSONRPCError as _JSONRPCError,
        JSONRPCRequest as _JSONRPCRequest,
        JSONRPCResponse as _JSONRPCResponse,
    )
    _HAS_RELAY = True
except ImportError:  # pragma: no cover
    _HAS_RELAY = False

# clangd requests that a client is expected to answer.  (window/showMessage
# and window/showDocument are notifications and need no relay.)
_SERVER_REQUEST_METHODS = frozenset({"window/workDoneProgress/create", "window/showMessageRequest"})


class _RelayReadStream:
    """Read-stream proxy: relay clangd's client-bound requests over MCP.

    A relayed request is rewritten into an MCP client request (elicit which
    action to pick) and the client's answer is turned back into a plain LSP
    response for clangd.  The SDK's own protocol sees only messages it can
    handle, so no METHOD_NOT_FOUND is ever sent to clangd.
    """

    def __init__(self, inner, client: "LSPClient") -> None:
        self._inner = inner
        self._client = client
        self._pending: dict[str, _JSONRPCRequest] = {}  # MCP id -> clangd request
        self._queue: asyncio.Queue = asyncio.Queue()
        self._inner_iter = None  # created lazily, reused across __anext__ calls

    # async-iterator protocol
    def __aiter__(self):
        return self

    async def __anext__(self):
        # Relayed requests are handed out before the next raw message; raw
        # messages that are themselves relays are consumed here.
        if not self._queue.empty():
            return self._queue.get_nowait()
        while True:
            item = await self._next_raw()
            if await self._handle(item):
                if not self._queue.empty():
                    return self._queue.get_nowait()
                continue
            return item

    async def _next_raw(self):
        """Pull one message from the wrapped stream (which stays open across awaits)."""
        if self._inner_iter is None:
            self._inner_iter = self._inner.__aiter__()
        try:
            return await self._inner_iter.__anext__()
        except StopAsyncIteration:
            raise StopAsyncIteration from None

    # context-manager protocol (the SDK wraps the stream in `async with`)
    async def __aenter__(self):
        enter = getattr(self._inner, "__aenter__", None)
        if enter is not None:
            await enter()
        return self

    async def __aexit__(self, *exc):
        exit_ = getattr(self._inner, "__aexit__", None)
        return await exit_(*exc) if exit_ is not None else None

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()

    async def _handle(self, item):
        """Relay `item` if it is a client-bound clangd message; True if swallowed."""
        msg = getattr(item, "message", None)
        if isinstance(msg, _JSONRPCRequest) and msg.method in _SERVER_REQUEST_METHODS:
            if msg.method == "window/workDoneProgress/create":
                # Answer locally: the only thing clangd needs is a success reply.
                await self._client._send({"jsonrpc": "2.0", "id": msg.id, "result": None})
            else:
                await self._relay_show_message_request(msg)
            return True
        if isinstance(msg, (_JSONRPCResponse, _JSONRPCError)):
            clangd_req = self._pending.pop(str(msg.id), None)
            if clangd_req is not None:
                await self._client._send(_answered(clangd_req, msg))
                return True
        return False

    async def _relay_show_message_request(self, msg: _JSONRPCRequest) -> None:
        """Ask the MCP client which action to pick; its answer is relayed later."""
        params = msg.params or {}
        actions = params.get("actions") or []
        schema: dict = {"type": "object", "properties": {}, "required": []}
        if actions:
            titles = [a.get("title", "") for a in actions]
            schema = {"type": "object",
                      "properties": {"action": {"type": "string", "enum": titles, "title": "Action"}},
                      "required": ["action"]}
        mcp_id = f"clangd-{msg.id}"
        self._pending[mcp_id] = msg
        self._queue.put_nowait(_SessionMessage(_JSONRPCRequest(
            jsonrpc="2.0", id=mcp_id, method="elicitation/create",
            params={"message": params.get("message", ""), "requestedSchema": schema},
        )))


def _answered(clangd_req: _JSONRPCRequest, answer) -> dict:
    """Convert an MCP client answer into the LSP response for `clangd_req`."""
    if isinstance(answer, _JSONRPCError):
        return {"jsonrpc": "2.0", "id": clangd_req.id, "result": None}
    result = answer.result or {}
    content = result.get("content") if isinstance(result, dict) else None
    title = content.get("action") if isinstance(content, dict) else None
    if not title:
        return {"jsonrpc": "2.0", "id": clangd_req.id, "result": None}
    return {"jsonrpc": "2.0", "id": clangd_req.id,
            "result": {"title": title,
                       "message": (clangd_req.params or {}).get("message", "")}}


def _install_relay(app: MCPServer, client: "LSPClient") -> None:
    """Wrap the app's low-level runner so clangd->client requests are answered."""
    if not _HAS_RELAY:  # pragma: no cover
        logging.getLogger(__name__).warning(
            "MCP SDK lacks the relay hooks; server-initiated messages are not relayed")
        return
    lowlevel = getattr(app, "_lowlevel_server", None)
    run = getattr(lowlevel, "run", None)
    if run is None:  # e.g. tests that replace the app with a mock
        return

    async def run_with_relay(read_stream, write_stream, initialization_options, *a, **kw):
        return await run(_RelayReadStream(read_stream, client), write_stream,
                         initialization_options, *a, **kw)

    lowlevel.run = run_with_relay  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Seed-file selection
# ---------------------------------------------------------------------------
#
# Opening one source file makes clangd build an index of that translation unit
# and guarantees the background indexer has work to do.  The file is taken from
# compile_commands.json (which this tool requires anyway); `--seed-file`
# overrides the automatic choice.

_SOURCE_SUFFIXES = (".cpp", ".cc", ".cxx", ".c++", ".c")
_COMPILE_COMMANDS_FILE = "compile_commands.json"


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:  # different drives
        return False


def _seed_from_compile_commands(compile_commands_dir: str | None, workspace_dir: str) -> str | None:
    """First existing workspace source listed in compile_commands.json.

    The database lives in `--compile-commands-dir` when given, otherwise in the
    workspace root (or its `build/` directory, where CMake puts it by default).
    """
    candidates: list[str] = []
    if compile_commands_dir:
        candidates.append(os.path.join(compile_commands_dir, _COMPILE_COMMANDS_FILE))
    else:
        candidates.append(os.path.join(workspace_dir, _COMPILE_COMMANDS_FILE))
        candidates.append(os.path.join(workspace_dir, "build", _COMPILE_COMMANDS_FILE))
    for db_path in candidates:
        try:
            entries = json.loads(pathlib.Path(db_path).read_text(errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            directory = entry.get("directory") or os.path.dirname(db_path)
            file_ = entry.get("file")
            if not file_:
                continue
            source = os.path.normpath(file_ if os.path.isabs(file_) else os.path.join(directory, file_))
            if source.endswith(_SOURCE_SUFFIXES) and os.path.isfile(source) \
                    and _is_within(source, workspace_dir):
                return source
    return None


def _pick_seed_file(workspace_dir: str, compile_commands_dir: str | None) -> str | None:
    """Choose the file to open at startup (an explicit --seed-file wins)."""
    if args.seed_file:
        seed = os.path.abspath(args.seed_file)
        if not os.path.isfile(seed):
            logger.warning("Seed file %s does not exist", seed)
        return seed
    seed = _seed_from_compile_commands(compile_commands_dir, workspace_dir)
    if seed is None:
        logger.warning(
            "No usable entry in compile_commands.json; cannot pick a seed file. "
            "clangd will still index the project in the background (set "
            "--compile-commands-dir or --seed-file if this is unexpected)")
    else:
        logger.info("Auto-selected seed file: %s", seed)
    return seed


# ---------------------------------------------------------------------------
# Startup / index readiness
# ---------------------------------------------------------------------------

_index_task: asyncio.Task[bool] | None = None  # the single background readiness wait


def _ensure_index_waiter() -> asyncio.Task[bool]:
    """Return the background readiness wait, starting it on first use."""
    global _index_task
    if _index_task is None or _index_task.done():
        assert lsp is not None
        _index_task = asyncio.create_task(
            lsp.wait_for_index(timeout=args.index_timeout_seconds),
            name="lsp-indexer",
        )
    return _index_task


async def _await_index(timeout: float | None = None) -> None:
    """Bounded wait for index readiness, shared with the background wait.

    Tools call this before index-dependent queries so a cold start does not
    silently return partial data.  It costs nothing once the index is ready
    (the common case - clangd finishes its first cycle within a second of
    startup) and is capped (default `--index-wait-seconds`) so a call never
    blocks for a full cold build.
    """
    if lsp is None or lsp.index_ready:
        return
    task = _ensure_index_waiter()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout or args.index_wait_seconds)
    except asyncio.TimeoutError:
        pass


@asynccontextmanager
async def lifespan(server: MCPServer):
    """Start clangd when the MCP server starts; shut it down on exit.

    Startup never blocks on indexing: clangd is started and (if one can be
    found) a seed file is opened, while readiness is tracked in the
    background.  Index-dependent tool calls take a short bounded wait.
    """
    global lsp, _index_task

    workspace_dir = os.path.abspath(args.workspace_dir)
    clangd_cmd = [args.clangd, "--log=error", "--background-index"]
    if args.compile_commands_dir:
        clangd_cmd.append(f"--compile-commands-dir={args.compile_commands_dir}")

    lsp = LSPClient()
    _install_relay(server, lsp)
    await lsp.start(clangd_cmd)
    await lsp.initialize(workspace_dir)

    seed = _pick_seed_file(workspace_dir, args.compile_commands_dir)
    if seed:
        await lsp.open_file(seed)
    # Track readiness even without a seed: with --background-index clangd
    # indexes the database anyway and reports progress.
    _ensure_index_waiter()

    try:
        yield
    finally:
        if _index_task is not None:
            _index_task.cancel()
        _index_task = None
        await lsp.shutdown()


mcp = MCPServer("clangd", lifespan=lifespan, log_level=args.log_level)

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_location(loc: dict) -> str:
    """Return 'path:line:col' (1-based) from an LSP Location dict."""
    path = uri_to_path(loc.get("uri", ""))
    start = loc.get("range", {}).get("start", {})
    line = start.get("line", 0) + 1
    col = start.get("character", 0) + 1
    return f"{path}:{line}:{col}"


def _source_context(path: str, line_0: int, context: int = 4) -> str:
    """Return source lines around line_0 (0-based) with a marker on that line."""
    try:
        lines = pathlib.Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return "(could not read file)"
    lo = max(0, line_0 - context)
    hi = min(len(lines), line_0 + context + 1)
    out = []
    for i in range(lo, hi):
        marker = ">>>" if i == line_0 else "   "
        out.append(f"{marker} {i + 1:5d} | {lines[i]}")
    return "\n".join(out)


def _format_hover_contents(contents) -> str:
    """Format LSP hover contents (MarkupContent, MarkedString, string, or array)."""
    if not contents:
        return ""
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        # Both MarkupContent {kind, value} and MarkedString {language, value} have "value"
        return contents.get("value", "")
    if isinstance(contents, list):
        parts = []
        for item in contents:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("value", ""))
        return "\n\n".join(p for p in parts if p)
    return str(contents)


def _format_hierarchy_item(item: dict) -> str:
    """Format a CallHierarchyItem or TypeHierarchyItem as '[Kind] name  path:line'."""
    name = item.get("name", "?")
    kind = SYMBOL_KINDS.get(item.get("kind", 0), "Unknown")
    uri = item.get("uri", "")
    path = uri_to_path(uri)
    line = item.get("range", {}).get("start", {}).get("line", 0) + 1
    return f"[{kind}] {name}  {path}:{line}"


def _format_doc_symbols(symbols: list[dict], indent: int = 0) -> list[str]:
    """Recursively format DocumentSymbol or SymbolInformation list."""
    lines = []
    prefix = "  " * indent
    for sym in symbols:
        name = sym.get("name", "?")
        kind = SYMBOL_KINDS.get(sym.get("kind", 0), "Unknown")
        if "selectionRange" in sym:
            # DocumentSymbol — has range, selectionRange, optional children
            start = sym.get("selectionRange", {}).get("start", {})
            line = start.get("line", 0) + 1
            col = start.get("character", 0) + 1
            lines.append(f"{prefix}[{kind}] {name}  line {line}:{col}")
            children = sym.get("children") or []
            if children:
                lines.extend(_format_doc_symbols(children, indent + 1))
        else:
            # SymbolInformation — has location
            loc = sym.get("location", {})
            loc_str = _format_location(loc)
            container = sym.get("containerName") or ""
            qualifier = f" (in {container})" if container else ""
            lines.append(f"{prefix}[{kind}] {name}{qualifier}  {loc_str}")
    return lines


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

_INDEXING_WARNING = (
    "Warning: clangd is still indexing the project. Results may be incomplete.\n\n"
)


def _indexing() -> bool:
    """True while clangd is (still) building its background index."""
    return lsp is not None and getattr(lsp, "indexing", False)


def _still_indexing(query: str) -> str:
    """Message for an empty result returned while the index is not ready."""
    return (
        f"{_INDEXING_WARNING}"
        f"clangd is still indexing the project, so '{query}' produced no results yet. "
        "Retry shortly; results become complete once indexing finishes."
    )


def _indexing_notice() -> str:
    """Short notice for successful results that may be incomplete."""
    return _INDEXING_WARNING if _indexing() else ""


@mcp.tool()
async def find_symbol(query: str) -> str:
    """Search for C/C++ symbols (functions, classes, variables, enums, etc.) by name across the entire codebase.

    Use this tool as your primary entry point when you know a symbol's name but don't know where it's defined or how it's used.
    It supports partial and fuzzy matching, so you don't need the exact name.
    Returns matching symbols with their kind, container, and file location.
    """
    assert lsp is not None
    await _await_index()
    if _indexing():
        # A cold index can legitimately answer [] rather than the truth, so
        # don't let callers mistake this for "symbol does not exist".
        return _still_indexing(query)
    symbols = await lsp.workspace_symbol(query)
    if not symbols:
        return f"No symbols found matching '{query}'."

    lines = [f"Found {len(symbols)} symbol(s) matching '{query}':\n"]
    for sym in symbols[:50]:
        name = sym.get("name", "?")
        kind = SYMBOL_KINDS.get(sym.get("kind", 0), "Unknown")
        container = sym.get("containerName") or ""
        location = sym.get("location", {})
        loc_str = _format_location(location)
        qualifier = f" (in {container})" if container else ""
        lines.append(f"  [{kind}] {name}{qualifier}\n    {loc_str}")

    if len(symbols) > 50:
        lines.append(f"\n  … and {len(symbols) - 50} more (refine your query)")

    return _indexing_notice() + "\n".join(lines)


@mcp.tool()
async def get_definition(symbol_name: str) -> str:
    """Find and show the exact source code definition of a C/C++ symbol.

    Use this tool when you want to read the actual implementation of a function, class, or variable.
    It resolves the symbol by name and jumps directly to the definition (e.g., in a .cpp file),
    even if you only have a reference to its declaration (e.g., in a .h file).
    Returns a source snippet centered on the definition site.
    """
    assert lsp is not None
    await _await_index()
    if _indexing():
        # A cold index can legitimately answer [] rather than the truth, so
        # don't let callers mistake this for "symbol does not exist".
        return _still_indexing(symbol_name)
    symbols = await lsp.workspace_symbol(symbol_name)
    if not symbols:
        return f"No symbols found matching '{symbol_name}'."

    # Prefer an exact name match; otherwise use the first result.
    exact = [s for s in symbols if s.get("name") == symbol_name]
    sym = exact[0] if exact else symbols[0]

    decl_loc = sym.get("location", {})
    decl_uri = decl_loc.get("uri", "")
    decl_path = uri_to_path(decl_uri)
    decl_start = decl_loc.get("range", {}).get("start", {})
    decl_line = decl_start.get("line", 0)
    decl_char = decl_start.get("character", 0)

    # Open the file so clangd can resolve cross-references.
    await lsp.open_file(decl_path)

    defs = await lsp.definition(decl_path, decl_line, decl_char)

    if defs:
        def_loc = defs[0]
        def_path = uri_to_path(def_loc.get("uri", decl_uri))
        def_start = def_loc.get("range", {}).get("start", {})
        def_line = def_start.get("line", 0)
        loc_str = _format_location(def_loc)
        snippet = _source_context(def_path, def_line)
        return _indexing_notice() + f"Definition of '{sym['name']}' at {loc_str}:\n\n{snippet}"
    else:
        # Fall back to the symbol's own declared location.
        loc_str = _format_location(decl_loc)
        snippet = _source_context(decl_path, decl_line)
        return _indexing_notice() + f"'{sym['name']}' at {loc_str}:\n\n{snippet}"


@mcp.tool()
async def find_references(symbol_name: str) -> str:
    """Find all usages of a C/C++ symbol across the entire codebase.

    Use this tool to understand the impact of changing a symbol, or to see real-world examples
    of how a function or class is used in different contexts.
    It returns a list of every reference clangd knows about, grouped by file.
    """
    assert lsp is not None
    await _await_index()
    if _indexing():
        # A cold index can legitimately answer [] rather than the truth, so
        # don't let callers mistake this for "symbol does not exist".
        return _still_indexing(symbol_name)
    symbols = await lsp.workspace_symbol(symbol_name)
    if not symbols:
        return f"No symbols found matching '{symbol_name}'."

    exact = [s for s in symbols if s.get("name") == symbol_name]
    sym = exact[0] if exact else symbols[0]

    decl_loc = sym.get("location", {})
    decl_path = uri_to_path(decl_loc.get("uri", ""))
    decl_start = decl_loc.get("range", {}).get("start", {})
    decl_line = decl_start.get("line", 0)
    decl_char = decl_start.get("character", 0)

    await lsp.open_file(decl_path)

    refs = await lsp.references(decl_path, decl_line, decl_char)
    if not refs:
        return _indexing_notice() + f"No references found for '{symbol_name}'."

    # Group by file path, sorted by line within each file.
    by_file: dict[str, list[dict]] = {}
    for ref in refs:
        file_path = uri_to_path(ref.get("uri", ""))
        by_file.setdefault(file_path, []).append(ref)

    lines = [f"Found {len(refs)} reference(s) to '{sym['name']}':\n"]
    for file_path in sorted(by_file):
        lines.append(f"\n{file_path}:")
        try:
            src_lines = pathlib.Path(file_path).read_text(errors="replace").splitlines()
        except OSError:
            src_lines = []
        for ref in sorted(by_file[file_path],
                          key=lambda r: r.get("range", {}).get("start", {}).get("line", 0)):
            start = ref.get("range", {}).get("start", {})
            line_0 = start.get("line", 0)
            col_0 = start.get("character", 0)
            src = src_lines[line_0].strip() if line_0 < len(src_lines) else ""
            lines.append(f"  {line_0 + 1}:{col_0 + 1}  {src}")

    return _indexing_notice() + "\n".join(lines)


@mcp.tool()
async def get_type_info(symbol_name: str) -> str:
    """Show type information, signature, and documentation for a C/C++ symbol.

    Use this tool for a quick overview of a symbol (like a "hover" in an IDE).
    It's perfect for checking function signatures, variable types, or reading
    Doxygen-style documentation comments without opening the full source file.
    """
    assert lsp is not None
    await _await_index()
    if _indexing():
        # A cold index can legitimately answer [] rather than the truth, so
        # don't let callers mistake this for "symbol does not exist".
        return _still_indexing(symbol_name)
    symbols = await lsp.workspace_symbol(symbol_name)
    if not symbols:
        return f"No symbols found matching '{symbol_name}'."

    exact = [s for s in symbols if s.get("name") == symbol_name]
    sym = exact[0] if exact else symbols[0]

    decl_loc = sym.get("location", {})
    decl_path = uri_to_path(decl_loc.get("uri", ""))
    decl_start = decl_loc.get("range", {}).get("start", {})
    decl_line = decl_start.get("line", 0)
    decl_char = decl_start.get("character", 0)

    await lsp.open_file(decl_path)

    hover = await lsp.hover(decl_path, decl_line, decl_char)
    if not hover:
        return _indexing_notice() + f"No type information available for '{sym['name']}'."

    contents = _format_hover_contents(hover.get("contents", ""))
    loc_str = _format_location(decl_loc)
    return _indexing_notice() + f"Type info for '{sym['name']}' at {loc_str}:\n\n{contents}"


@mcp.tool()
async def find_implementations(symbol_name: str) -> str:
    """Find all concrete implementations of a C/C++ virtual method or interface.

    Use this tool when you are working with polymorphic code or inheritance.
    If you have a base class method, this tool will show you all the overridden versions
    in derived classes.
    """
    assert lsp is not None
    await _await_index()
    if _indexing():
        # A cold index can legitimately answer [] rather than the truth, so
        # don't let callers mistake this for "symbol does not exist".
        return _still_indexing(symbol_name)
    symbols = await lsp.workspace_symbol(symbol_name)
    if not symbols:
        return f"No symbols found matching '{symbol_name}'."

    exact = [s for s in symbols if s.get("name") == symbol_name]
    sym = exact[0] if exact else symbols[0]

    decl_loc = sym.get("location", {})
    decl_path = uri_to_path(decl_loc.get("uri", ""))
    decl_start = decl_loc.get("range", {}).get("start", {})
    decl_line = decl_start.get("line", 0)
    decl_char = decl_start.get("character", 0)

    await lsp.open_file(decl_path)

    impls = await lsp.implementation(decl_path, decl_line, decl_char)
    if not impls:
        return _indexing_notice() + f"No implementations found for '{sym['name']}'."

    by_file: dict[str, list[dict]] = {}
    for impl in impls:
        file_path = uri_to_path(impl.get("uri", ""))
        by_file.setdefault(file_path, []).append(impl)

    lines = [f"Found {len(impls)} implementation(s) of '{sym['name']}':\n"]
    for file_path in sorted(by_file):
        lines.append(f"\n{file_path}:")
        try:
            src_lines = pathlib.Path(file_path).read_text(errors="replace").splitlines()
        except OSError:
            src_lines = []
        for impl in sorted(by_file[file_path],
                           key=lambda r: r.get("range", {}).get("start", {}).get("line", 0)):
            start = impl.get("range", {}).get("start", {})
            line_0 = start.get("line", 0)
            col_0 = start.get("character", 0)
            src = src_lines[line_0].strip() if line_0 < len(src_lines) else ""
            lines.append(f"  {line_0 + 1}:{col_0 + 1}  {src}")

    return _indexing_notice() + "\n".join(lines)


@mcp.tool()
async def get_callers(symbol_name: str) -> str:
    """Find all functions that call a given C/C++ function ("Who calls this?").

    Use this tool to trace the execution flow backwards. It's essential for
    understanding how a function is integrated into the larger system and
    identifying all possible entry points to it.
    """
    assert lsp is not None
    await _await_index()
    if _indexing():
        # A cold index can legitimately answer [] rather than the truth, so
        # don't let callers mistake this for "symbol does not exist".
        return _still_indexing(symbol_name)
    symbols = await lsp.workspace_symbol(symbol_name)
    if not symbols:
        return f"No symbols found matching '{symbol_name}'."

    exact = [s for s in symbols if s.get("name") == symbol_name]
    sym = exact[0] if exact else symbols[0]

    decl_loc = sym.get("location", {})
    decl_path = uri_to_path(decl_loc.get("uri", ""))
    decl_start = decl_loc.get("range", {}).get("start", {})
    decl_line = decl_start.get("line", 0)
    decl_char = decl_start.get("character", 0)

    await lsp.open_file(decl_path)

    items = await lsp.prepare_call_hierarchy(decl_path, decl_line, decl_char)
    if not items:
        return _indexing_notice() + f"'{sym['name']}' is not callable or not found in the call hierarchy."

    root_item = items[0]
    calls = await lsp.incoming_calls(root_item)
    if not calls:
        return _indexing_notice() + f"No callers found for '{sym['name']}'."

    lines = [f"Found {len(calls)} caller(s) of '{sym['name']}':\n"]
    for call in calls:
        caller = call.get("from", {})
        caller_str = _format_hierarchy_item(caller)
        from_ranges = call.get("fromRanges", [])
        caller_path = uri_to_path(caller.get("uri", ""))
        try:
            src_lines = pathlib.Path(caller_path).read_text(errors="replace").splitlines()
        except OSError:
            src_lines = []
        lines.append(f"\n  {caller_str}")
        for rng in from_ranges:
            start = rng.get("start", {})
            line_0 = start.get("line", 0)
            col_0 = start.get("character", 0)
            src = src_lines[line_0].strip() if line_0 < len(src_lines) else ""
            lines.append(f"    called at {line_0 + 1}:{col_0 + 1}  {src}")

    return _indexing_notice() + "\n".join(lines)


@mcp.tool()
async def get_callees(symbol_name: str) -> str:
    """Find all functions called by a given C/C++ function ("What does this call?").

    Use this tool to explore the dependencies and internal logic of a function.
    It helps you see which other parts of the codebase this function relies on.
    """
    assert lsp is not None
    await _await_index()
    if _indexing():
        # A cold index can legitimately answer [] rather than the truth, so
        # don't let callers mistake this for "symbol does not exist".
        return _still_indexing(symbol_name)
    symbols = await lsp.workspace_symbol(symbol_name)
    if not symbols:
        return f"No symbols found matching '{symbol_name}'."

    exact = [s for s in symbols if s.get("name") == symbol_name]
    sym = exact[0] if exact else symbols[0]

    decl_loc = sym.get("location", {})
    decl_path = uri_to_path(decl_loc.get("uri", ""))
    decl_start = decl_loc.get("range", {}).get("start", {})
    decl_line = decl_start.get("line", 0)
    decl_char = decl_start.get("character", 0)

    await lsp.open_file(decl_path)

    items = await lsp.prepare_call_hierarchy(decl_path, decl_line, decl_char)
    if not items:
        return _indexing_notice() + f"'{sym['name']}' is not callable or not found in the call hierarchy."

    root_item = items[0]
    calls = await lsp.outgoing_calls(root_item)
    if not calls:
        return _indexing_notice() + f"No callees found for '{sym['name']}'."

    try:
        src_lines = pathlib.Path(decl_path).read_text(errors="replace").splitlines()
    except OSError:
        src_lines = []

    lines = [f"Found {len(calls)} callee(s) of '{sym['name']}':\n"]
    for call in calls:
        callee = call.get("to", {})
        callee_str = _format_hierarchy_item(callee)
        from_ranges = call.get("fromRanges", [])
        lines.append(f"\n  {callee_str}")
        for rng in from_ranges:
            start = rng.get("start", {})
            line_0 = start.get("line", 0)
            col_0 = start.get("character", 0)
            src = src_lines[line_0].strip() if line_0 < len(src_lines) else ""
            lines.append(f"    called at {line_0 + 1}:{col_0 + 1}  {src}")

    return _indexing_notice() + "\n".join(lines)


@mcp.tool()
async def list_file_symbols(file_path: str) -> str:
    """List all symbols (functions, classes, variables, etc.) defined in a specific file.

    Use this tool to get a high-level overview of a file's structure. It's like an
    "Outline" view in an IDE, helping you quickly see what's inside a .cpp or .h file.
    Expects an absolute or relative path to a C/C++ file.
    """
    assert lsp is not None
    abs_path = os.path.abspath(file_path)
    await lsp.open_file(abs_path)

    symbols = await lsp.document_symbol(abs_path)
    if not symbols:
        return f"No symbols found in '{abs_path}'."

    sym_lines = _format_doc_symbols(symbols)
    header = f"Symbols in '{abs_path}' ({len(sym_lines)} entries):\n"
    return _indexing_notice() + header + "\n".join(sym_lines)


@mcp.tool()
async def get_type_hierarchy(symbol_name: str) -> str:
    """Show the full type hierarchy (supertypes and subtypes) of a C/C++ class or struct.

    Use this tool when you want to understand the class inheritance structure.
    It will show you both the base classes (parents) and any derived classes (children)
    of the given type.
    """
    assert lsp is not None
    await _await_index()
    if _indexing():
        # A cold index can legitimately answer [] rather than the truth, so
        # don't let callers mistake this for "symbol does not exist".
        return _still_indexing(symbol_name)
    symbols = await lsp.workspace_symbol(symbol_name)
    if not symbols:
        return f"No symbols found matching '{symbol_name}'."

    exact = [s for s in symbols if s.get("name") == symbol_name]
    sym = exact[0] if exact else symbols[0]

    decl_loc = sym.get("location", {})
    decl_path = uri_to_path(decl_loc.get("uri", ""))
    decl_start = decl_loc.get("range", {}).get("start", {})
    decl_line = decl_start.get("line", 0)
    decl_char = decl_start.get("character", 0)

    await lsp.open_file(decl_path)

    items = await lsp.prepare_type_hierarchy(decl_path, decl_line, decl_char)
    if not items:
        return _indexing_notice() + f"'{sym['name']}' is not a type or not found in the type hierarchy."

    root_item = items[0]
    supertypes, subtypes = await asyncio.gather(
        lsp.type_supertypes(root_item),
        lsp.type_subtypes(root_item),
    )

    lines = [f"Type hierarchy for '{sym['name']}':\n"]

    lines.append(f"\nSupertypes ({len(supertypes)}):")
    if supertypes:
        for t in supertypes:
            lines.append(f"  {_format_hierarchy_item(t)}")
    else:
        lines.append("  (none)")

    lines.append(f"\nSubtypes ({len(subtypes)}):")
    if subtypes:
        for t in subtypes:
            lines.append(f"  {_format_hierarchy_item(t)}")
    else:
        lines.append("  (none)")

    return _indexing_notice() + "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # A real run reports indexing at INFO so a cold start is visible in the
    # MCP client's log (the default level stays WARNING).
    logging.getLogger("lsp_client").info(
        "clangd-mcp starting (workspace=%s)", os.path.abspath(args.workspace_dir))
    mcp.run()


if __name__ == "__main__":
    main()
