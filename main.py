"""MCP server that exposes clangd capabilities as tools for Claude.

Usage:
    python main.py [--clangd PATH] [--compile-commands-dir DIR] [--workspace-dir DIR]

The server speaks the Model Context Protocol (MCP) over stdio and bridges to
clangd via the Language Server Protocol (LSP).

Exposed tools:
    find_symbol      -- search workspace symbols by name
    get_definition   -- find where a symbol is defined (with source preview)
    find_references  -- find all usages of a symbol
"""

import argparse
import asyncio
import logging
import os
import pathlib
import sys
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

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
                   help="Source file to open at startup to trigger clangd background indexing")
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

lsp: LSPClient | None = None


@asynccontextmanager
async def lifespan(server: FastMCP):
    """Start clangd when the MCP server starts; shut it down on exit."""
    global lsp

    workspace_dir = os.path.abspath(args.workspace_dir)
    clangd_cmd = [args.clangd, "--log=error", "--background-index"]
    if args.compile_commands_dir:
        clangd_cmd.append(f"--compile-commands-dir={args.compile_commands_dir}")

    lsp = LSPClient()
    await lsp.start(clangd_cmd)
    await lsp.initialize(workspace_dir)

    if args.seed_file:
        await lsp.open_file(os.path.abspath(args.seed_file))
        asyncio.create_task(lsp.wait_for_index(), name="lsp-indexer")

    try:
        yield
    finally:
        await lsp.shutdown()


mcp = FastMCP("clangd", lifespan=lifespan)

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


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

_INDEXING_MSG = (
    "clangd is still indexing the project. Please retry in a moment."
)


def _indexing() -> bool:
    """True if a seed file was provided but the index isn't ready yet."""
    return args.seed_file is not None and lsp is not None and not lsp.index_ready


@mcp.tool()
async def find_symbol(query: str) -> str:
    """Search for C/C++ symbols (functions, classes, variables, …) by name.

    Returns matching symbols with their kind, container, and file location.
    Supports partial and fuzzy name matching.
    """
    assert lsp is not None
    if _indexing():
        return _INDEXING_MSG
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

    return "\n".join(lines)


@mcp.tool()
async def get_definition(symbol_name: str) -> str:
    """Show the definition of a C/C++ symbol.

    Looks up the symbol by name, then asks clangd for its definition location
    (which may differ from the declaration in a header).  Returns the source
    snippet at the definition site.
    """
    assert lsp is not None
    if _indexing():
        return _INDEXING_MSG
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
        return f"Definition of '{sym['name']}' at {loc_str}:\n\n{snippet}"
    else:
        # Fall back to the symbol's own declared location.
        loc_str = _format_location(decl_loc)
        snippet = _source_context(decl_path, decl_line)
        return f"'{sym['name']}' at {loc_str}:\n\n{snippet}"


@mcp.tool()
async def find_references(symbol_name: str) -> str:
    """Find all usages of a C/C++ symbol across the codebase.

    Looks up the symbol by name, then collects every reference clangd knows
    about (including the declaration).  Results are grouped by file.
    """
    assert lsp is not None
    if _indexing():
        return _INDEXING_MSG
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
        return f"No references found for '{symbol_name}'."

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

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
