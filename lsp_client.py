"""Minimal LSP client that speaks to clangd over JSON-RPC 2.0 on stdin/stdout.

Message framing follows the LSP specification:
    Content-Length: <byte-count>\r\n
    \r\n
    <utf-8 json payload>
"""

import asyncio
import json
import logging
import os
import pathlib
from typing import Any

logger = logging.getLogger(__name__)

SYMBOL_KINDS = {
    1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class",
    6: "Method", 7: "Property", 8: "Field", 9: "Constructor", 10: "Enum",
    11: "Interface", 12: "Function", 13: "Variable", 14: "Constant",
    15: "String", 16: "Number", 17: "Boolean", 18: "Array", 19: "Object",
    20: "Key", 21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event",
    25: "Operator", 26: "TypeParameter",
}


def path_to_uri(path: str) -> str:
    return "file://" + os.path.abspath(path)


def uri_to_path(uri: str) -> str:
    if uri.startswith("file://"):
        return uri[7:]
    return uri


class LSPClient:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._open_files: set[str] = set()  # tracks URIs already opened

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def start(self, command: list[str]) -> None:
        """Launch clangd and start the background reader task."""
        logger.info("Starting LSP server: %s", command)
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(self._read_loop(), name="lsp-reader")
        asyncio.create_task(self._log_stderr(), name="lsp-stderr")

    async def initialize(self, workspace_dir: str) -> None:
        """Perform the LSP initialize / initialized handshake."""
        workspace_uri = path_to_uri(workspace_dir)
        result = await self.request("initialize", {
            "processId": os.getpid(),
            "clientInfo": {"name": "mcp-clangd", "version": "0.1.0"},
            "rootUri": workspace_uri,
            "workspaceFolders": [{"uri": workspace_uri, "name": "workspace"}],
            "capabilities": {
                "workspace": {
                    "symbol": {"symbolKind": {"valueSet": list(range(1, 27))}},
                },
                "textDocument": {
                    "definition": {"linkSupport": False},
                    "references": {},
                },
            },
        })
        server_info = result.get("serverInfo", {}) if result else {}
        logger.info("LSP initialized: %s", server_info)
        await self.notify("initialized", {})

    async def shutdown(self) -> None:
        """Gracefully stop the LSP server."""
        try:
            await asyncio.wait_for(self.request("shutdown"), timeout=5.0)
            await self.notify("exit")
        except Exception as exc:
            logger.debug("Shutdown error (ignored): %s", exc)
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except Exception:
                self._process.kill()

    # ------------------------------------------------------------------ #
    # Transport                                                            #
    # ------------------------------------------------------------------ #

    async def _read_loop(self) -> None:
        """Background task: read messages from clangd and resolve pending futures."""
        assert self._process and self._process.stdout
        while True:
            try:
                msg = await self._read_message()
            except (asyncio.IncompleteReadError, EOFError):
                logger.debug("LSP stdout closed")
                break
            except Exception as exc:
                logger.error("LSP read error: %s", exc)
                break
            if msg is None:
                break
            self._dispatch(msg)

    async def _read_message(self) -> dict[str, Any] | None:
        """Read exactly one framed JSON-RPC message."""
        assert self._process and self._process.stdout
        content_length: int | None = None
        # Read headers until blank line
        while True:
            raw = await self._process.stdout.readline()
            if not raw:
                return None
            line = raw.decode("utf-8").rstrip()
            if line == "":
                break
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":", 1)[1].strip())

        if content_length is None:
            return None

        body = await self._process.stdout.readexactly(content_length)
        msg = json.loads(body.decode("utf-8"))
        logger.debug("← %s", msg.get("method") or f"response id={msg.get('id')}")
        return msg

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route an incoming message to the waiting future (if any)."""
        msg_id = msg.get("id")
        if msg_id is not None and msg_id in self._pending:
            future = self._pending.pop(msg_id)
            if not future.done():
                if "error" in msg:
                    future.set_exception(RuntimeError(f"LSP error: {msg['error']}"))
                else:
                    future.set_result(msg.get("result"))
        # Notifications and server-initiated requests are ignored for now.

    async def _send(self, msg: dict[str, Any]) -> None:
        """Write one framed JSON-RPC message to clangd's stdin."""
        assert self._process and self._process.stdin
        body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        logger.debug("→ %s", msg.get("method") or f"response id={msg.get('id')}")
        self._process.stdin.write(header + body)
        await self._process.stdin.drain()

    async def _log_stderr(self) -> None:
        assert self._process and self._process.stderr
        while not self._process.stderr.at_eof():
            line = await self._process.stderr.readline()
            if line:
                logger.debug("clangd: %s", line.decode(errors="replace").rstrip())

    # ------------------------------------------------------------------ #
    # JSON-RPC primitives                                                  #
    # ------------------------------------------------------------------ #

    async def request(self, method: str, params: Any = None) -> Any:
        """Send a request and await the response (timeout: 30 s)."""
        self._next_id += 1
        req_id = self._next_id
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        await self._send(msg)
        return await asyncio.wait_for(future, timeout=30.0)

    async def notify(self, method: str, params: Any = None) -> None:
        """Send a notification (no response expected)."""
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    # ------------------------------------------------------------------ #
    # File management                                                      #
    # ------------------------------------------------------------------ #

    async def open_file(self, path: str) -> None:
        """Send textDocument/didOpen; no-op if already open."""
        uri = path_to_uri(path)
        if uri in self._open_files:
            return
        try:
            text = pathlib.Path(path).read_text(errors="replace")
        except OSError as exc:
            logger.warning("Cannot read %s: %s", path, exc)
            return
        suffix = pathlib.Path(path).suffix.lower()
        lang = "c" if suffix == ".c" else "cpp"
        await self.notify("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": lang, "version": 1, "text": text},
        })
        self._open_files.add(uri)

    # ------------------------------------------------------------------ #
    # LSP queries                                                          #
    # ------------------------------------------------------------------ #

    async def workspace_symbol(self, query: str) -> list[dict]:
        """workspace/symbol — search symbols by name pattern."""
        result = await self.request("workspace/symbol", {"query": query})
        return result or []

    async def definition(self, path: str, line: int, character: int) -> list[dict]:
        """textDocument/definition — resolve definition location(s)."""
        result = await self.request("textDocument/definition", {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": line, "character": character},
        })
        if not result:
            return []
        return result if isinstance(result, list) else [result]

    async def references(self, path: str, line: int, character: int) -> list[dict]:
        """textDocument/references — find all reference locations."""
        result = await self.request("textDocument/references", {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": True},
        })
        return result or []
