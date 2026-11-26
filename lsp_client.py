# Copyright 2026 The clangd-mcp developers
# SPDX-License-Identifier: MIT

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
import re
import time
import urllib.parse
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Token clangd uses for its background-index progress notifications.  Only
# progress with this token is interpreted as indexing state; every other
# work-done progress (e.g. per-file parsing) is ignored.
INDEX_PROGRESS_TOKEN = "backgroundIndexProgress"

# How long clangd may stay silent before its index is assumed usable: a cached
# on-disk index is loaded without any $/progress, and a begin whose end never
# arrives means the cycle was abandoned.  A prompt clangd always reports (or
# finishes) well inside this window, so this is a constant of the protocol
# rather than a tuning knob.
INDEX_PROBE_SECONDS = 5.0

IndexState = Literal["unknown", "indexing", "ready"]

SYMBOL_KINDS = {
    1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class",
    6: "Method", 7: "Property", 8: "Field", 9: "Constructor", 10: "Enum",
    11: "Interface", 12: "Function", 13: "Variable", 14: "Constant",
    15: "String", 16: "Number", 17: "Boolean", 18: "Array", 19: "Object",
    20: "Key", 21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event",
    25: "Operator", 26: "TypeParameter",
}


def path_to_uri(path: str) -> str:
    # pathlib.Path.as_uri() emits an RFC 8089 file URI on every platform:
    #   POSIX   -> file:///home/user/foo.h
    #   Windows -> file:///C:/Users/foo.h   (triple slash, forward slashes,
    #              drive letter after the leading slash)
    return pathlib.Path(os.path.abspath(path)).as_uri()


def uri_to_path(uri: str) -> str:
    if not uri.startswith("file://"):
        return uri
    path = urllib.parse.unquote(urllib.parse.urlparse(uri).path)
    # Windows file URIs arrive as "/C:/dir/file"; strip the spurious leading
    # slash and normalise separators. POSIX paths are returned untouched so
    # that "file:///tmp/foo.h" still maps to "/tmp/foo.h" on every platform.
    if re.match(r"^/[A-Za-z]:/", path):
        return os.path.normpath(path[1:])
    return path


class LSPClient:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._open_files: set[str] = set()  # tracks URIs already opened

        # Background-index state machine (see wait_for_index):
        #   unknown  -> nothing known yet
        #   indexing -> a begin arrived, waiting for end
        #   ready    -> the last seen indexing cycle finished
        #
        # `_index_rev` changes on every transition; waiters poll it instead of
        # a set-once Event, so a later begin re-arms "indexing" and a later end
        # re-marks "ready" even if a previous wait already finished.
        self._index_state: IndexState = "unknown"
        self._index_rev = 0
        self._index_last_progress: float | None = None  # monotonic time of last $/progress
        self._index_last_report: float | None = None    # monotonic time of last "report"
        self._index_ready_assumed: bool = False
        self._last_activity: float | None = None        # monotonic time of any LSP message

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
            "clientInfo": {"name": "clangd-mcp", "version": "0.1.0"},
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
                # clangd only reports background-index progress over $/progress
                # if the client advertises workDoneProgress (LLVM D73218).
                "window": {"workDoneProgress": True},
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
    # Background-index readiness                                           #
    # ------------------------------------------------------------------ #

    @property
    def index_ready(self) -> bool:
        """True once the current indexing cycle finished (or was assumed cached)."""
        return self._index_state == "ready"

    @property
    def indexing(self) -> bool:
        """True while clangd announced indexing that has not finished yet."""
        return self._index_state == "indexing"

    @property
    def index_ready_assumed(self) -> bool:
        """True if readiness was inferred without seeing any progress cycle."""
        return self._index_ready_assumed

    def _set_index_state(self, state: IndexState, *, assumed: bool = False) -> None:
        """Transition the index state machine and wake waiters."""
        if state != self._index_state:
            logger.debug("index state: %s -> %s", self._index_state, state)
            self._index_state = state
        self._index_ready_assumed = assumed
        self._index_rev += 1

    def _activity(self) -> float | None:
        """Monotonic time of the last sign of life from clangd (LSP or progress)."""
        candidates = [t for t in (self._last_activity, self._index_last_progress) if t is not None]
        return max(candidates) if candidates else None

    def _stalled(self, probe: float) -> bool:
        """True if indexing is announced but clangd stopped doing anything.

        A begin promises an end; it is only abandoned when no progress (and no
        LSP traffic at all) has been seen for ``probe`` seconds, i.e. clangd is
        not working on it any more.  A busy clangd keeps the cycle alive.
        """
        if self._index_state != "indexing" or probe <= 0:
            return False
        last = self._activity()
        return last is not None and time.monotonic() - last >= probe

    def _assume_ready(self, reason: str) -> None:
        logger.info(
            "Assuming clangd's index is usable (%s); tools may see partial "
            "symbol data if indexing continues in the background", reason)
        self._set_index_state("ready", assumed=True)

    async def wait_for_index(
        self, timeout: float = 600.0, probe: float = INDEX_PROBE_SECONDS,
    ) -> bool:
        """Wait until clangd's background index is usable (bounded).

        clangd reports background indexing as repeated ``$/progress``
        begin/report/end cycles with the token ``backgroundIndexProgress``.
        Cycles repeat while the queue drains and refills, so ``end`` marks the
        *current* cycle ready and a later ``begin`` re-arms indexing.

        Waiting is transition-driven - it never sleeps out a fixed delay while
        the index is already usable:

          * ready already -> return immediately.  (A single-shot MCP client
            calls one tool right after startup, by which time clangd has
            usually finished its cycle: this must not cost a probe interval.)
          * indexing -> wait until ``end``.  A ``begin`` is clangd's promise
            that an ``end`` follows, so wait for it (up to ``timeout``) instead
            of guessing from a fixed delay.  Only a begin whose clangd went
            completely silent for ``probe`` seconds is written off as stalled.
          * unknown -> wait for the first begin/report; if none arrives within
            ``probe`` (``INDEX_PROBE_SECONDS``) the on-disk index was most
            likely loaded from cache (clangd sends no progress for that), so
            assume ready.

        Returns True if the index is ready (or assumed ready), False if the
        wait timed out while still indexing.
        """
        started = time.monotonic()
        deadline = started + max(0.0, timeout)
        while True:
            if self._index_state == "ready":
                return True

            if self._index_state == "indexing":
                # A begin was announced: wait for its end (that is the signal).
                # If clangd goes completely silent mid-cycle for `probe`
                # seconds, write the cycle off rather than block forever.
                if self._stalled(probe):
                    self._assume_ready(f"indexing progress stalled for {probe:.0f}s")
                    return True
                if time.monotonic() >= deadline:
                    logger.warning(
                        "clangd background index still running after %.0fs; "
                        "returning partial results", timeout)
                    return False
            else:
                # Nothing seen yet: cached index, or clangd is still starting
                # up.  A cached index sends no progress at all, so the probe is
                # the only upper bound on waiting.
                if time.monotonic() - started >= probe:
                    self._assume_ready(f"no indexing progress within {probe:.0f}s")
                    return True
                if time.monotonic() >= deadline:
                    logger.warning(
                        "no indexing progress after %.0fs; proceeding", timeout)
                    return False

            rev = self._index_rev
            try:
                await self._wait_rev_change(rev, deadline)
            except asyncio.TimeoutError:
                pass

    async def _wait_rev_change(self, rev: int, deadline: float) -> None:
        """Sleep until the index revision changes or the deadline passes."""
        while self._index_rev == rev and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

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
        self._last_activity = time.monotonic()
        msg_id = msg.get("id")
        if msg_id is not None and msg_id in self._pending:
            future = self._pending.pop(msg_id)
            if not future.done():
                if "error" in msg:
                    future.set_exception(RuntimeError(f"LSP error: {msg['error']}"))
                else:
                    future.set_result(msg.get("result"))
        elif msg.get("method") == "$/progress":
            self._on_progress(msg.get("params") or {})
        elif msg_id is not None and "method" in msg:
            # Server-initiated request.  clangd sends
            # window/workDoneProgress/create before its first progress
            # notification and silently stops reporting if it gets no reply.
            # The MCP transport answers unknown methods with an error; reply
            # explicitly here so the client also works when driven directly.
            if msg["method"] == "window/workDoneProgress/create":
                asyncio.create_task(
                    self._send({"jsonrpc": "2.0", "id": msg_id, "result": None}),
                    name="lsp-workdone-reply",
                )
            else:
                logger.debug("Ignoring server request %s", msg["method"])

    def _on_progress(self, params: dict[str, Any]) -> None:
        """Apply one $/progress notification to the index state machine."""
        if params.get("token") != INDEX_PROGRESS_TOKEN:
            return  # other progress stream (e.g. per-file parsing) - ignore
        value = params.get("value") or {}
        kind = value.get("kind")
        now = time.monotonic()
        # Any progress on the indexing token is a sign of life for _activity().
        self._index_last_progress = now
        if kind == "begin":
            if self._index_state != "indexing":
                logger.info("clangd started background indexing")
            self._set_index_state("indexing")
        elif kind == "report":
            self._index_last_report = now
            pct = value.get("percentage")
            logger.debug("clangd indexing: %s%s", value.get("message", ""),
                         f" ({pct}%)" if pct is not None else "")
        elif kind == "end":
            logger.info("clangd finished a background-indexing cycle")
            self._index_last_report = now
            self._set_index_state("ready")

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
        while True:
            chunk = await self._process.stderr.read(4096)
            if not chunk:
                break
            logger.debug("clangd: %s", chunk.decode(errors="replace").rstrip())

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

    async def hover(self, path: str, line: int, character: int) -> dict | None:
        """textDocument/hover — get type/doc info at a position."""
        result = await self.request("textDocument/hover", {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": line, "character": character},
        })
        return result

    async def implementation(self, path: str, line: int, character: int) -> list[dict]:
        """textDocument/implementation — find implementations of a virtual method or interface."""
        result = await self.request("textDocument/implementation", {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": line, "character": character},
        })
        if not result:
            return []
        return result if isinstance(result, list) else [result]

    async def document_symbol(self, path: str) -> list[dict]:
        """textDocument/documentSymbol — list all symbols in a file."""
        result = await self.request("textDocument/documentSymbol", {
            "textDocument": {"uri": path_to_uri(path)},
        })
        return result or []

    async def prepare_call_hierarchy(self, path: str, line: int, character: int) -> list[dict]:
        """textDocument/prepareCallHierarchy — prepare a call hierarchy item at position."""
        result = await self.request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": line, "character": character},
        })
        return result or []

    async def incoming_calls(self, item: dict) -> list[dict]:
        """callHierarchy/incomingCalls — find callers of a call hierarchy item."""
        return (await self.request("callHierarchy/incomingCalls", {"item": item})) or []

    async def outgoing_calls(self, item: dict) -> list[dict]:
        """callHierarchy/outgoingCalls — find callees of a call hierarchy item."""
        return (await self.request("callHierarchy/outgoingCalls", {"item": item})) or []

    async def prepare_type_hierarchy(self, path: str, line: int, character: int) -> list[dict]:
        """textDocument/prepareTypeHierarchy — prepare a type hierarchy item at position."""
        result = await self.request("textDocument/prepareTypeHierarchy", {
            "textDocument": {"uri": path_to_uri(path)},
            "position": {"line": line, "character": character},
        })
        return result or []

    async def type_supertypes(self, item: dict) -> list[dict]:
        """typeHierarchy/supertypes — find supertypes of a type hierarchy item."""
        return (await self.request("typeHierarchy/supertypes", {"item": item})) or []

    async def type_subtypes(self, item: dict) -> list[dict]:
        """typeHierarchy/subtypes — find subtypes of a type hierarchy item."""
        return (await self.request("typeHierarchy/subtypes", {"item": item})) or []
