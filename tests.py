# Copyright 2026 The clangd-mcp developers
# SPDX-License-Identifier: MIT

"""Tests for clangd-mcp.

Runs without a real clangd binary by driving the LSP client with a fake
in-process server that echoes back canned LSP responses.

Run with:
    uv run pytest tests.py -v
or:
    uv run python tests.py
"""

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from lsp_client import LSPClient, path_to_uri, uri_to_path, SYMBOL_KINDS

# server.py parses argv at import time; running `python tests.py [selector]`
# must not leak unittest's selectors into that parser.
def _import_server():
    """Import server.py with benign argv (it parses argv at import time)."""
    import sys
    saved = sys.argv
    sys.argv = saved[:1]
    try:
        import server
    finally:
        sys.argv = saved
    return server


# ---------------------------------------------------------------------------
# Helpers for building fake LSP messages
# ---------------------------------------------------------------------------

def _frame(payload: dict) -> bytes:
    """Wrap a dict in LSP Content-Length framing."""
    body = json.dumps(payload).encode()
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


def _response(req_id: int, result) -> bytes:
    return _frame({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error_response(req_id: int, message: str) -> bytes:
    return _frame({"jsonrpc": "2.0", "id": req_id,
                   "error": {"code": -32603, "message": message}})


# ---------------------------------------------------------------------------
# Helpers for building fake asyncio streams
# ---------------------------------------------------------------------------

def _make_stream(*frames: bytes):
    """Return a fake asyncio StreamReader pre-loaded with LSP frames."""
    reader = asyncio.StreamReader()
    for frame in frames:
        reader.feed_data(frame)
    reader.feed_eof()
    return reader


# ---------------------------------------------------------------------------
# Unit tests: LSP transport helpers
# ---------------------------------------------------------------------------

class TestUriHelpers(unittest.TestCase):
    def test_path_roundtrip(self):
        path = "/home/user/project/src/foo.cpp"
        self.assertEqual(uri_to_path(path_to_uri(path)), path)

    def test_uri_to_path_strips_prefix(self):
        self.assertEqual(uri_to_path("file:///tmp/foo.h"), "/tmp/foo.h")

    def test_uri_to_path_passthrough(self):
        self.assertEqual(uri_to_path("/already/a/path"), "/already/a/path")

    def test_path_to_uri_is_rfc8089(self):
        # Regression: a file URI must use the empty-authority "file:///" form
        # and never contain backslashes, otherwise clangd rejects it as an
        # "unresolvable URI" on Windows.
        uri = path_to_uri(os.path.abspath("foo.h"))
        self.assertTrue(uri.startswith("file:///"))
        self.assertNotIn("\\", uri)


class TestMessageFraming(unittest.IsolatedAsyncioTestCase):
    """Drive _read_message directly with a crafted stream."""

    async def _client_with_stream(self, reader: asyncio.StreamReader) -> LSPClient:
        client = LSPClient()
        # Attach a fake process whose stdout is our reader
        proc = MagicMock()
        proc.stdout = reader
        proc.stderr = _make_stream()          # empty stderr
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        client._process = proc
        return client

    async def test_read_single_message(self):
        payload = {"jsonrpc": "2.0", "id": 1, "result": {"key": "value"}}
        reader = _make_stream(_frame(payload))
        client = await self._client_with_stream(reader)
        msg = await client._read_message()
        self.assertEqual(msg, payload)

    async def test_read_returns_none_on_eof(self):
        reader = asyncio.StreamReader()
        reader.feed_eof()
        client = await self._client_with_stream(reader)
        msg = await client._read_message()
        self.assertIsNone(msg)

    async def test_read_multiple_messages(self):
        p1 = {"jsonrpc": "2.0", "id": 1, "result": "a"}
        p2 = {"jsonrpc": "2.0", "id": 2, "result": "b"}
        reader = _make_stream(_frame(p1), _frame(p2))
        client = await self._client_with_stream(reader)
        self.assertEqual(await client._read_message(), p1)
        self.assertEqual(await client._read_message(), p2)

    async def test_send_writes_framed_message(self):
        reader = asyncio.StreamReader()
        reader.feed_eof()
        client = await self._client_with_stream(reader)

        written = bytearray()
        client._process.stdin.write = lambda data: written.extend(data)

        await client._send({"jsonrpc": "2.0", "id": 1, "method": "test"})

        data = bytes(written)
        header, _, body = data.partition(b"\r\n\r\n")
        content_length = int(header.split(b"Content-Length: ")[1])
        self.assertEqual(content_length, len(body))
        msg = json.loads(body)
        self.assertEqual(msg["method"], "test")


# ---------------------------------------------------------------------------
# Unit tests: request / response correlation
# ---------------------------------------------------------------------------

class TestRequestResponse(unittest.IsolatedAsyncioTestCase):

    def _make_client_with_response(self, result) -> tuple[LSPClient, list]:
        """Return a client whose _send captures outgoing data and auto-resolves."""
        client = LSPClient()
        sent_messages: list[dict] = []

        async def fake_send(msg):
            sent_messages.append(msg)
            # Immediately resolve the pending future as if clangd replied
            req_id = msg.get("id")
            if req_id is not None and req_id in client._pending:
                future = client._pending[req_id]
                if not future.done():
                    future.set_result(result)

        client._send = fake_send
        return client, sent_messages

    async def test_request_returns_result(self):
        client, sent = self._make_client_with_response({"answer": 42})
        result = await client.request("foo/bar", {"x": 1})
        self.assertEqual(result, {"answer": 42})
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["method"], "foo/bar")
        self.assertEqual(sent[0]["params"], {"x": 1})

    async def test_request_raises_on_error(self):
        client = LSPClient()

        async def fake_send(msg):
            req_id = msg.get("id")
            if req_id in client._pending:
                client._pending[req_id].set_exception(
                    RuntimeError("LSP error: something went wrong")
                )

        client._send = fake_send
        with self.assertRaises(RuntimeError):
            await client.request("bad/method")

    async def test_notify_sends_no_id(self):
        client = LSPClient()
        sent_messages: list[dict] = []

        async def fake_send(msg):
            sent_messages.append(msg)

        client._send = fake_send
        await client.notify("textDocument/didOpen", {"x": 1})
        self.assertEqual(len(sent_messages), 1)
        self.assertNotIn("id", sent_messages[0])
        self.assertEqual(sent_messages[0]["method"], "textDocument/didOpen")

    async def test_dispatch_resolves_future(self):
        client = LSPClient()
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        client._pending[7] = future
        client._dispatch({"jsonrpc": "2.0", "id": 7, "result": "hello"})
        self.assertEqual(await future, "hello")

    async def test_dispatch_ignores_unknown_id(self):
        client = LSPClient()
        # Should not raise even if ID is not in pending
        client._dispatch({"jsonrpc": "2.0", "id": 999, "result": "x"})


# ---------------------------------------------------------------------------
# Unit tests: LSP feature methods
# ---------------------------------------------------------------------------

class TestLSPFeatureMethods(unittest.IsolatedAsyncioTestCase):

    def _client(self, responses: dict) -> LSPClient:
        """Client whose request() returns canned responses by method name."""
        client = LSPClient()
        async def fake_request(method, params=None):
            return responses.get(method)
        async def fake_notify(method, params=None):
            pass
        client.request = fake_request
        client.notify = fake_notify
        return client

    async def test_workspace_symbol_returns_list(self):
        symbols = [{"name": "Foo", "kind": 5, "location": {"uri": "file:///a.cpp", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}}}}]
        client = self._client({"workspace/symbol": symbols})
        result = await client.workspace_symbol("Foo")
        self.assertEqual(result, symbols)

    async def test_workspace_symbol_empty(self):
        client = self._client({"workspace/symbol": None})
        result = await client.workspace_symbol("Missing")
        self.assertEqual(result, [])

    async def test_definition_wraps_single_result(self):
        loc = {"uri": "file:///b.cpp", "range": {"start": {"line": 10, "character": 0}, "end": {"line": 10, "character": 5}}}
        client = self._client({"textDocument/definition": loc})
        result = await client.definition("/a.cpp", 0, 0)
        self.assertEqual(result, [loc])

    async def test_definition_passes_through_list(self):
        locs = [{"uri": "file:///b.cpp", "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 1}}}]
        client = self._client({"textDocument/definition": locs})
        result = await client.definition("/a.cpp", 0, 0)
        self.assertEqual(result, locs)

    async def test_references_returns_list(self):
        refs = [
            {"uri": "file:///x.cpp", "range": {"start": {"line": 5, "character": 2}, "end": {"line": 5, "character": 7}}},
            {"uri": "file:///y.cpp", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}}},
        ]
        client = self._client({"textDocument/references": refs})
        result = await client.references("/a.cpp", 0, 0)
        self.assertEqual(result, refs)

    async def test_open_file_sends_did_open(self):
        client = LSPClient()
        notifications: list[dict] = []

        async def fake_notify(method, params=None):
            notifications.append({"method": method, "params": params})

        client.notify = fake_notify

        import tempfile, pathlib
        with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False) as f:
            f.write(b"int main() {}")
            tmp_path = f.name

        await client.open_file(tmp_path)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["method"], "textDocument/didOpen")
        self.assertEqual(notifications[0]["params"]["textDocument"]["languageId"], "cpp")

        # Second open should be a no-op (already tracked)
        await client.open_file(tmp_path)
        self.assertEqual(len(notifications), 1)

        import os
        os.unlink(tmp_path)

    async def test_hover_returns_result(self):
        hover = {"contents": {"kind": "markdown", "value": "**int** foo"}}
        client = self._client({"textDocument/hover": hover})
        result = await client.hover("/a.cpp", 5, 3)
        self.assertEqual(result, hover)

    async def test_hover_returns_none(self):
        client = self._client({"textDocument/hover": None})
        result = await client.hover("/a.cpp", 0, 0)
        self.assertIsNone(result)

    async def test_document_symbol_returns_list(self):
        syms = [{"name": "Foo", "kind": 5, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 10, "character": 1}}, "selectionRange": {"start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 9}}}]
        client = self._client({"textDocument/documentSymbol": syms})
        result = await client.document_symbol("/a.cpp")
        self.assertEqual(result, syms)

    async def test_document_symbol_empty(self):
        client = self._client({"textDocument/documentSymbol": None})
        result = await client.document_symbol("/a.cpp")
        self.assertEqual(result, [])

    async def test_implementation_wraps_single_result(self):
        loc = {"uri": "file:///b.cpp", "range": {"start": {"line": 5, "character": 0}, "end": {"line": 5, "character": 5}}}
        client = self._client({"textDocument/implementation": loc})
        result = await client.implementation("/a.cpp", 0, 0)
        self.assertEqual(result, [loc])

    async def test_prepare_call_hierarchy_returns_list(self):
        item = {"name": "myFunc", "kind": 12, "uri": "file:///a.cpp", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 6}}, "selectionRange": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 6}}}
        client = self._client({"textDocument/prepareCallHierarchy": [item]})
        result = await client.prepare_call_hierarchy("/a.cpp", 0, 0)
        self.assertEqual(result, [item])

    async def test_incoming_calls_returns_list(self):
        call = {"from": {"name": "caller", "kind": 12, "uri": "file:///b.cpp", "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 6}}, "selectionRange": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 6}}}, "fromRanges": [{"start": {"line": 3, "character": 2}, "end": {"line": 3, "character": 8}}]}
        client = self._client({"callHierarchy/incomingCalls": [call]})
        result = await client.incoming_calls({"name": "myFunc"})
        self.assertEqual(result, [call])

    async def test_outgoing_calls_returns_list(self):
        call = {"to": {"name": "helper", "kind": 12, "uri": "file:///c.cpp", "range": {"start": {"line": 5, "character": 0}, "end": {"line": 5, "character": 6}}, "selectionRange": {"start": {"line": 5, "character": 0}, "end": {"line": 5, "character": 6}}}, "fromRanges": [{"start": {"line": 10, "character": 4}, "end": {"line": 10, "character": 10}}]}
        client = self._client({"callHierarchy/outgoingCalls": [call]})
        result = await client.outgoing_calls({"name": "myFunc"})
        self.assertEqual(result, [call])

    async def test_prepare_type_hierarchy_returns_list(self):
        item = {"name": "Base", "kind": 5, "uri": "file:///a.h", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 10, "character": 1}}, "selectionRange": {"start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 10}}}
        client = self._client({"textDocument/prepareTypeHierarchy": [item]})
        result = await client.prepare_type_hierarchy("/a.h", 0, 6)
        self.assertEqual(result, [item])

    async def test_type_supertypes_returns_list(self):
        super_item = {"name": "Animal", "kind": 5, "uri": "file:///animal.h", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 5, "character": 1}}, "selectionRange": {"start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 12}}}
        client = self._client({"typeHierarchy/supertypes": [super_item]})
        result = await client.type_supertypes({"name": "Dog"})
        self.assertEqual(result, [super_item])

    async def test_type_subtypes_returns_list(self):
        sub_item = {"name": "Poodle", "kind": 5, "uri": "file:///poodle.h", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 5, "character": 1}}, "selectionRange": {"start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 12}}}
        client = self._client({"typeHierarchy/subtypes": [sub_item]})
        result = await client.type_subtypes({"name": "Dog"})
        self.assertEqual(result, [sub_item])


# ---------------------------------------------------------------------------
# Unit tests: background-index readiness ($/progress state machine)
# ---------------------------------------------------------------------------

def _progress(kind: str, token: str = "backgroundIndexProgress", **value) -> dict:
    return {"jsonrpc": "2.0", "method": "$/progress",
            "params": {"token": token, "value": {"kind": kind, **value}}}


class TestIndexReadiness(unittest.IsolatedAsyncioTestCase):

    def test_initial_state_unknown(self):
        client = LSPClient()
        self.assertFalse(client.index_ready)
        self.assertFalse(client.indexing)

    async def test_dispatch_begin_report_end(self):
        client = LSPClient()
        client._dispatch(_progress("begin", title="Indexing"))
        self.assertTrue(client.indexing)
        self.assertFalse(client.index_ready)

        client._dispatch(_progress("report", message="70%", percentage=70))
        self.assertIsNotNone(client._index_last_report)
        self.assertTrue(client.indexing)

        client._dispatch(_progress("end", message="done", percentage=100))
        self.assertTrue(client.index_ready)
        self.assertFalse(client.indexing)

    async def test_rearm_after_end(self):
        # clangd sends repeated begin/report/end cycles as the queue drains:
        # a later begin must make the client "indexing" again.
        client = LSPClient()
        client._dispatch(_progress("begin"))
        client._dispatch(_progress("end"))
        self.assertTrue(client.index_ready)
        client._dispatch(_progress("begin"))
        self.assertFalse(client.index_ready)
        self.assertTrue(client.indexing)
        client._dispatch(_progress("end"))
        self.assertTrue(client.index_ready)

    async def test_ignores_other_tokens(self):
        client = LSPClient()
        client._dispatch(_progress("begin", token="textDocument/parse"))
        client._dispatch(_progress("end", token="textDocument/parse"))
        self.assertFalse(client.indexing)
        self.assertFalse(client.index_ready)
        self.assertEqual(client._index_state, "unknown")

    async def test_end_without_begin_marks_ready(self):
        # clangd may report the end of a cycle whose begin predates us.
        client = LSPClient()
        client._dispatch(_progress("begin", token="parse"))  # unrelated token
        self.assertEqual(client._index_state, "unknown")
        client._dispatch(_progress("end"))
        self.assertTrue(client.index_ready)

    async def test_wait_for_index_end_within_timeout(self):
        client = LSPClient()

        async def produce():
            await asyncio.sleep(0.05)
            client._dispatch(_progress("begin"))
            await asyncio.sleep(0.05)
            client._dispatch(_progress("report", percentage=50))
            await asyncio.sleep(0.05)
            client._dispatch(_progress("end"))

        task = asyncio.create_task(produce())
        result = await client.wait_for_index(timeout=5.0, probe=0.1)
        await task
        self.assertTrue(result)
        self.assertTrue(client.index_ready)

    async def test_wait_for_index_no_begin_probe(self):
        client = LSPClient()
        result = await client.wait_for_index(timeout=5.0, probe=0.1)
        self.assertTrue(result)
        self.assertTrue(client.index_ready)
        self.assertTrue(client.index_ready_assumed)
        self.assertEqual(client._index_state, "ready")

    async def test_wait_for_index_timeout_stays_indexing(self):
        client = LSPClient()
        client._dispatch(_progress("begin"))
        result = await client.wait_for_index(timeout=0.1, probe=0.0)
        self.assertFalse(result)
        self.assertTrue(client.indexing)
        self.assertFalse(client.index_ready)
        self.assertFalse(client.index_ready_assumed)

        # ...and a later end still flips it to ready for the next caller.
        client._dispatch(_progress("end"))
        self.assertTrue(client.index_ready)
        self.assertTrue(await client.wait_for_index(timeout=0.1, probe=0.05))

    async def test_initialize_advertises_work_done_progress(self):
        client = LSPClient()
        captured: dict = {}

        async def fake_request(method, params=None):
            captured["method"] = method
            captured["params"] = params
            return {"serverInfo": {"name": "clangd"}}

        async def fake_notify(method, params=None):
            pass

        client.request = fake_request
        client.notify = fake_notify
        await client.initialize("/tmp/ws")

        caps = captured["params"]["capabilities"]
        self.assertEqual(caps["window"]["workDoneProgress"], True)
        self.assertIn("symbol", caps["workspace"])
        self.assertIn("definition", caps["textDocument"])
        self.assertIn("references", caps["textDocument"])

    async def test_replies_to_work_done_progress_create(self):
        client = LSPClient()
        sent: list[dict] = []

        async def fake_send(msg):
            sent.append(msg)

        client._send = fake_send
        client._dispatch({"jsonrpc": "2.0", "id": 42,
                          "method": "window/workDoneProgress/create",
                          "params": {"token": "backgroundIndexProgress"}})
        await asyncio.sleep(0)  # let the reply task run
        await asyncio.sleep(0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0], {"jsonrpc": "2.0", "id": 42, "result": None})

    async def test_dispatch_response_to_server_request_is_dropped(self):
        # A response to a server-initiated request (has id, no method) must not
        # crash or be treated as a notification.
        client = LSPClient()
        client._dispatch({"jsonrpc": "2.0", "id": 0, "result": None})


# ---------------------------------------------------------------------------
# Unit tests: server-side indexing notices
# ---------------------------------------------------------------------------

class TestServerIndexingNotices(unittest.IsolatedAsyncioTestCase):

    def _mock(self, indexing: bool, **results):
        mock = AsyncMock(spec=LSPClient)
        mock.indexing = indexing
        mock.index_ready = not indexing
        mock.wait_for_index.return_value = not indexing
        for method, result in results.items():
            getattr(mock, method).return_value = result
        return mock

    async def test_find_symbol_warns_while_indexing(self):
        import server
        with patch.object(server, "lsp", self._mock(True, workspace_symbol=[])):
            result = await server.find_symbol("Ghost")
        self.assertIn("still indexing", result)
        self.assertNotIn("No symbols found", result)

    async def test_find_symbol_no_warning_when_ready(self):
        import server
        sym = {"name": "Foo", "kind": 5, "containerName": "",
               "location": {"uri": path_to_uri("/proj/foo.cpp"),
                            "range": {"start": {"line": 0, "character": 0},
                                      "end": {"line": 0, "character": 3}}}}
        with patch.object(server, "lsp", self._mock(False, workspace_symbol=[sym])):
            result = await server.find_symbol("Foo")
        self.assertNotIn("still indexing", result)
        self.assertIn("[Class] Foo", result)

    async def test_find_symbol_still_warns_on_partial_results(self):
        import server
        sym = {"name": "Foo", "kind": 5, "containerName": "",
               "location": {"uri": path_to_uri("/proj/foo.cpp"),
                            "range": {"start": {"line": 0, "character": 0},
                                      "end": {"line": 0, "character": 3}}}}
        # Symbols came back but indexing is still running: results are partial.
        with patch.object(server, "lsp", self._mock(True, workspace_symbol=[sym])):
            result = await server.find_symbol("Foo")
        self.assertIn("still indexing", result)
        self.assertIn("Foo", result)

    async def test_tools_wait_for_index_once(self):
        import server
        mock = self._mock(True, workspace_symbol=[])  # still indexing
        # A never-finishing wait, so `_await_index` can't replace the task.
        async def _hang(*a, **kw):
            await asyncio.Event().wait()
        mock.wait_for_index.side_effect = _hang
        with patch.object(server, "lsp", mock), \
                patch.object(server, "_index_task", None), \
                patch.object(server.args, "index_wait_seconds", 0.01):
            await server.find_symbol("x")
            await server.find_symbol("y")
        self.assertEqual(mock.wait_for_index.await_count, 1)

    async def test_ready_index_costs_no_wait(self):
        # Regression: a single-shot client (mcp-cli) calls one tool right after
        # startup.  Once indexing has finished the call must not wait at all.
        import server
        mock = self._mock(False, workspace_symbol=[])
        with patch.object(server, "lsp", mock), \
                patch.object(server, "_index_task", None), \
                patch.object(server.args, "index_wait_seconds", 30.0):
            started = asyncio.get_running_loop().time()
            await server.find_symbol("x")
            elapsed = asyncio.get_running_loop().time() - started
        self.assertLess(elapsed, 1.0)
        self.assertEqual(mock.wait_for_index.await_count, 0)

    async def test_get_type_info_warns_while_indexing(self):
        import server
        with patch.object(server, "lsp", self._mock(True, workspace_symbol=[])):
            result = await server.get_type_info("Ghost")
        self.assertIn("still indexing", result)


# ---------------------------------------------------------------------------
# Unit tests: relay of server-initiated messages (clangd -> MCP client)
# ---------------------------------------------------------------------------

class _FakeStream:
    """Async-iterable message stream fed from a queue (None ends it)."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


class TestServerRequestRelay(unittest.IsolatedAsyncioTestCase):
    """clangd's client-bound requests must not die as METHOD_NOT_FOUND."""

    def setUp(self):
        _import_server()  # benign argv, before anything else touches argv

    def _client(self, sent: list):
        client = LSPClient()

        async def fake_send(msg):
            sent.append(msg)

        client._send = fake_send
        return client

    def _relay(self, client, items=()):
        """Return (relay, inner stream, list of relayed requests)."""
        import server
        stream = _FakeStream()
        for item in items:
            stream.queue.put_nowait(item)
        relay = server._RelayReadStream(stream, client)
        relayed: list = []
        original_put = relay._queue.put_nowait

        def record_put(item):
            relayed.append(item)
            original_put(item)

        relay._queue.put_nowait = record_put
        return relay, stream, relayed

    async def test_work_done_progress_create_answered_locally(self):
        from mcp_types import JSONRPCRequest
        from mcp.shared.message import SessionMessage

        sent: list = []
        relay, stream, relayed = self._relay(self._client(sent), [
            SessionMessage(JSONRPCRequest(jsonrpc="2.0", id=0,
                                          method="window/workDoneProgress/create",
                                          params={"token": "backgroundIndexProgress"})),
            None,
        ])
        passed = [item async for item in relay]
        self.assertEqual(passed, [])
        self.assertEqual(relayed, [])
        self.assertEqual(sent, [{"jsonrpc": "2.0", "id": 0, "result": None}])

    async def test_show_message_request_relayed_and_answered(self):
        from mcp_types import JSONRPCRequest, JSONRPCResponse
        from mcp.shared.message import SessionMessage

        sent: list = []
        client = self._client(sent)
        relay, stream, relayed = self._relay(client, [
            SessionMessage(JSONRPCRequest(
                jsonrpc="2.0", id=1, method="window/showMessageRequest",
                params={"type": 2, "message": "index too old",
                        "actions": [{"title": "Dismiss"}, {"title": "Rebuild"}]})),
        ])

        async def drive():
            # Answer the relayed elicitation, then close the stream.
            while not relayed:
                await asyncio.sleep(0.005)
            elicit = relayed[0].message
            stream.queue.put_nowait(SessionMessage(JSONRPCResponse(
                jsonrpc="2.0", id=elicit.id,
                result={"action": "accept", "content": {"action": "Rebuild"}})))
            stream.queue.put_nowait(SessionMessage(JSONRPCRequest(
                jsonrpc="2.0", id=2, method="tools/call", params={})))
            stream.queue.put_nowait(None)

        driver = asyncio.create_task(drive())
        passed = [item async for item in relay]
        await asyncio.wait_for(driver, timeout=2)

        self.assertEqual([m.message.method for m in passed], ["elicitation/create", "tools/call"])
        schema = passed[0].message.params["requestedSchema"]["properties"]["action"]
        self.assertEqual(schema["enum"], ["Dismiss", "Rebuild"])
        self.assertEqual(sent, [{"jsonrpc": "2.0", "id": 1,
                                 "result": {"title": "Rebuild", "message": "index too old"}}])
        self.assertEqual(len(relayed), 1)

    async def test_rejected_elicit_answers_null(self):
        from mcp_types import JSONRPCRequest, JSONRPCResponse
        from mcp.shared.message import SessionMessage

        sent: list = []
        relay, stream, relayed = self._relay(self._client(sent), [
            SessionMessage(JSONRPCRequest(
                jsonrpc="2.0", id=3, method="window/showMessageRequest",
                params={"type": 1, "message": "bad compile flags"})),
        ])

        async def drive():
            while not relayed:
                await asyncio.sleep(0.005)
            stream.queue.put_nowait(SessionMessage(JSONRPCResponse(
                jsonrpc="2.0", id=relayed[0].message.id, result={"action": "decline"})))
            stream.queue.put_nowait(None)

        driver = asyncio.create_task(drive())
        [item async for item in relay]
        await asyncio.wait_for(driver, timeout=2)
        self.assertEqual(sent, [{"jsonrpc": "2.0", "id": 3, "result": None}])

    async def test_unrelated_messages_pass_through(self):
        from mcp_types import JSONRPCRequest
        from mcp.shared.message import SessionMessage

        sent: list = []
        msg = SessionMessage(JSONRPCRequest(jsonrpc="2.0", id=5, method="tools/call", params={}))
        relay, stream, _ = self._relay(self._client(sent), [msg, None])
        self.assertEqual([item async for item in relay], [msg])
        self.assertEqual(sent, [])


# ---------------------------------------------------------------------------
# Unit tests: seed-file auto-pick
# ---------------------------------------------------------------------------

class TestSeedPicking(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "src"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "build"), exist_ok=True)
        self.src = os.path.join(self.root, "src", "main.cpp")
        with open(self.src, "w") as f:
            f.write("int main() { return 0; }\n")
        self.header = os.path.join(self.root, "src", "main.h")
        with open(self.header, "w") as f:
            f.write("#pragma once\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_db(self, path: str, entries: list) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(entries, f)

    def test_explicit_compile_commands_dir(self):
        server = _import_server()
        build_dir = os.path.join(self.root, "build")
        self._write_db(os.path.join(build_dir, "compile_commands.json"), [
            {"directory": self.root, "file": "/elsewhere/other.cpp"},
            {"directory": os.path.join(self.root, "src"), "file": "main.cpp"},
        ])
        picked = server._seed_from_compile_commands(build_dir, self.root)
        self.assertEqual(picked, self.src)

    def test_compile_commands_in_workspace_root(self):
        server = _import_server()
        self._write_db(os.path.join(self.root, "compile_commands.json"),
                       [{"directory": self.root, "file": "src/main.cpp"}])
        self.assertEqual(server._seed_from_compile_commands(None, self.root), self.src)

    def test_compile_commands_in_build_dir(self):
        # CMake's default location when no --compile-commands-dir is given.
        server = _import_server()
        self._write_db(os.path.join(self.root, "build", "compile_commands.json"),
                       [{"directory": os.path.join(self.root, "src"), "file": "main.cpp"}])
        self.assertEqual(server._seed_from_compile_commands(None, self.root), self.src)

    def test_compile_commands_skips_missing_and_outside(self):
        server = _import_server()
        self._write_db(os.path.join(self.root, "compile_commands.json"), [
            {"directory": self.root, "file": "src/gone.cpp"},
            {"directory": "/tmp", "file": "/tmp/outside.cpp"},
            {"directory": self.root, "file": "src/main.cpp"},
        ])
        self.assertEqual(server._seed_from_compile_commands(None, self.root), self.src)

    def test_compile_commands_missing_or_invalid(self):
        server = _import_server()
        self.assertIsNone(server._seed_from_compile_commands(None, self.root))
        with open(os.path.join(self.root, "compile_commands.json"), "w") as f:
            f.write("{not json")
        self.assertIsNone(server._seed_from_compile_commands(None, self.root))

    def test_no_filesystem_fallback(self):
        # Without compile_commands.json there is no seed, even though a source
        # file sits in the workspace.
        server = _import_server()
        with patch.object(server.args, "seed_file", None):
            self.assertIsNone(server._pick_seed_file(self.root, None))

    def test_explicit_seed_file_wins(self):
        server = _import_server()
        with patch.object(server.args, "seed_file", self.src):
            self.assertEqual(server._pick_seed_file(self.root, None), self.src)

    def test_pick_uses_compile_commands(self):
        server = _import_server()
        self._write_db(os.path.join(self.root, "compile_commands.json"),
                       [{"directory": os.path.join(self.root, "src"), "file": "main.cpp"}])
        with patch.object(server.args, "seed_file", None):
            self.assertEqual(server._pick_seed_file(self.root, None), self.src)


# ---------------------------------------------------------------------------
# Integration-style tests: MCP tool handlers
# ---------------------------------------------------------------------------

class TestMCPTools(unittest.IsolatedAsyncioTestCase):
    """Test tool handler logic by patching the global lsp object in main."""

    def _mock_lsp(self, **method_results):
        # `indexing`/`index_ready` are plain properties on LSPClient, so they are
        # absent from a spec'd AsyncMock and read as truthy mock attributes.
        # Pin them so the tests see a finished index unless they say otherwise.
        mock = AsyncMock(spec=LSPClient)
        mock.indexing = False
        mock.index_ready = True
        mock.wait_for_index.return_value = True
        for method, result in method_results.items():
            getattr(mock, method).return_value = result
        return mock

    def _symbol(self, name, kind=12, container="", file="/proj/foo.cpp", line=10, char=0):
        return {
            "name": name,
            "kind": kind,
            "containerName": container,
            "location": {
                "uri": path_to_uri(file),
                "range": {"start": {"line": line, "character": char},
                          "end":   {"line": line, "character": char + len(name)}},
            },
        }

    async def test_find_symbol_found(self):
        import server
        sym = self._symbol("MyClass", kind=5)
        with patch.object(server, "lsp", self._mock_lsp(workspace_symbol=[sym])):
            result = await server.find_symbol("MyClass")
        self.assertIn("MyClass", result)
        self.assertIn("[Class]", result)
        self.assertIn("foo.cpp", result)

    async def test_find_symbol_not_found(self):
        import server
        with patch.object(server, "lsp", self._mock_lsp(workspace_symbol=[])):
            result = await server.find_symbol("Ghost")
        self.assertIn("No symbols found", result)

    async def test_find_symbol_caps_at_50(self):
        import server
        symbols = [self._symbol(f"sym_{i}") for i in range(60)]
        with patch.object(server, "lsp", self._mock_lsp(workspace_symbol=symbols)):
            result = await server.find_symbol("sym")
        self.assertIn("10 more", result)

    async def test_get_definition_exact_match_preferred(self):
        import server, tempfile, pathlib
        # Write a temp file so _source_context can read it
        with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False, mode="w") as f:
            for i in range(20):
                f.write(f"// line {i}\n")
            tmp = f.name

        sym_exact = self._symbol("myFunc", file=tmp, line=5)
        sym_other = self._symbol("myFuncHelper", file=tmp, line=15)
        def_loc = {"uri": path_to_uri(tmp),
                   "range": {"start": {"line": 5, "character": 0},
                             "end":   {"line": 5, "character": 6}}}

        mock_lsp = self._mock_lsp(
            workspace_symbol=[sym_other, sym_exact],
            definition=[def_loc],
        )
        mock_lsp.open_file = AsyncMock()

        with patch.object(server, "lsp", mock_lsp):
            result = await server.get_definition("myFunc")

        self.assertIn("myFunc", result)
        self.assertIn(tmp, result)

        import os; os.unlink(tmp)

    async def test_find_references_groups_by_file(self):
        import server, tempfile, pathlib
        with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False, mode="w") as f:
            for i in range(20):
                f.write(f"use_it_here();  // line {i}\n")
            tmp = f.name

        sym = self._symbol("use_it_here", file=tmp, line=0)
        refs = [
            {"uri": path_to_uri(tmp),
             "range": {"start": {"line": 2, "character": 4}, "end": {"line": 2, "character": 14}}},
            {"uri": path_to_uri(tmp),
             "range": {"start": {"line": 8, "character": 4}, "end": {"line": 8, "character": 14}}},
        ]
        mock_lsp = self._mock_lsp(workspace_symbol=[sym], references=refs)
        mock_lsp.open_file = AsyncMock()

        with patch.object(server, "lsp", mock_lsp):
            result = await server.find_references("use_it_here")

        self.assertIn("2 reference", result)
        self.assertIn(tmp, result)
        self.assertIn("3:", result)   # line 2 (0-based) → "3:" (1-based)
        self.assertIn("9:", result)   # line 8 → "9:"

        import os; os.unlink(tmp)

    async def test_find_references_not_found(self):
        import server
        with patch.object(server, "lsp", self._mock_lsp(workspace_symbol=[])):
            result = await server.find_references("nobody")
        self.assertIn("No symbols found", result)

    def _call_item(self, name, file="/proj/foo.cpp", line=0):
        """Build a minimal CallHierarchyItem / TypeHierarchyItem dict."""
        from lsp_client import path_to_uri
        uri = path_to_uri(file)
        rng = {"start": {"line": line, "character": 0}, "end": {"line": line, "character": len(name)}}
        return {"name": name, "kind": 12, "uri": uri, "range": rng, "selectionRange": rng}

    async def test_get_type_info_found(self):
        import server, tempfile
        with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False, mode="w") as f:
            for i in range(5):
                f.write(f"// line {i}\n")
            tmp = f.name

        sym = self._symbol("myVar", file=tmp, line=2)
        hover = {"contents": {"kind": "plaintext", "value": "int myVar"}}
        mock_lsp = self._mock_lsp(workspace_symbol=[sym], hover=hover)
        mock_lsp.open_file = AsyncMock()

        with patch.object(server, "lsp", mock_lsp):
            result = await server.get_type_info("myVar")

        self.assertIn("myVar", result)
        self.assertIn("int myVar", result)

        import os; os.unlink(tmp)

    async def test_get_type_info_no_symbol(self):
        import server
        with patch.object(server, "lsp", self._mock_lsp(workspace_symbol=[])):
            result = await server.get_type_info("Ghost")
        self.assertIn("No symbols found", result)

    async def test_get_type_info_no_hover(self):
        import server, tempfile
        with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False, mode="w") as f:
            f.write("int x;\n")
            tmp = f.name

        sym = self._symbol("x", file=tmp, line=0)
        mock_lsp = self._mock_lsp(workspace_symbol=[sym], hover=None)
        mock_lsp.open_file = AsyncMock()

        with patch.object(server, "lsp", mock_lsp):
            result = await server.get_type_info("x")

        self.assertIn("No type information", result)

        import os; os.unlink(tmp)

    async def test_find_implementations_found(self):
        import server, tempfile
        with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False, mode="w") as f:
            for i in range(10):
                f.write(f"void impl_{i}() {{}}\n")
            tmp = f.name

        sym = self._symbol("draw", file=tmp, line=0)
        impl_loc = {"uri": path_to_uri(tmp), "range": {"start": {"line": 3, "character": 5}, "end": {"line": 3, "character": 9}}}
        mock_lsp = self._mock_lsp(workspace_symbol=[sym], implementation=[impl_loc])
        mock_lsp.open_file = AsyncMock()

        with patch.object(server, "lsp", mock_lsp):
            result = await server.find_implementations("draw")

        self.assertIn("implementation", result)
        self.assertIn(tmp, result)

        import os; os.unlink(tmp)

    async def test_find_implementations_no_symbol(self):
        import server
        with patch.object(server, "lsp", self._mock_lsp(workspace_symbol=[])):
            result = await server.find_implementations("IShape")
        self.assertIn("No symbols found", result)

    async def test_get_callers_found(self):
        import server, tempfile
        with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False, mode="w") as f:
            for i in range(15):
                f.write(f"// line {i}\n")
            tmp = f.name

        sym = self._symbol("compute", file=tmp, line=0)
        root_item = self._call_item("compute", file=tmp, line=0)
        call = {
            "from": self._call_item("main", file=tmp, line=5),
            "fromRanges": [{"start": {"line": 7, "character": 4}, "end": {"line": 7, "character": 11}}],
        }
        mock_lsp = self._mock_lsp(
            workspace_symbol=[sym],
            prepare_call_hierarchy=[root_item],
            incoming_calls=[call],
        )
        mock_lsp.open_file = AsyncMock()

        with patch.object(server, "lsp", mock_lsp):
            result = await server.get_callers("compute")

        self.assertIn("caller", result)
        self.assertIn("main", result)

        import os; os.unlink(tmp)

    async def test_get_callers_not_callable(self):
        import server, tempfile
        with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False, mode="w") as f:
            f.write("int x;\n")
            tmp = f.name

        sym = self._symbol("x", file=tmp, line=0)
        mock_lsp = self._mock_lsp(workspace_symbol=[sym], prepare_call_hierarchy=[])
        mock_lsp.open_file = AsyncMock()

        with patch.object(server, "lsp", mock_lsp):
            result = await server.get_callers("x")

        self.assertIn("not callable", result)

        import os; os.unlink(tmp)

    async def test_get_callees_found(self):
        import server, tempfile
        with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False, mode="w") as f:
            for i in range(15):
                f.write(f"// line {i}\n")
            tmp = f.name

        sym = self._symbol("main", file=tmp, line=0)
        root_item = self._call_item("main", file=tmp, line=0)
        call = {
            "to": self._call_item("helper", file=tmp, line=8),
            "fromRanges": [{"start": {"line": 3, "character": 4}, "end": {"line": 3, "character": 10}}],
        }
        mock_lsp = self._mock_lsp(
            workspace_symbol=[sym],
            prepare_call_hierarchy=[root_item],
            outgoing_calls=[call],
        )
        mock_lsp.open_file = AsyncMock()

        with patch.object(server, "lsp", mock_lsp):
            result = await server.get_callees("main")

        self.assertIn("callee", result)
        self.assertIn("helper", result)

        import os; os.unlink(tmp)

    async def test_list_file_symbols(self):
        import server, tempfile
        with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False, mode="w") as f:
            f.write("int foo();\nclass Bar {};\n")
            tmp = f.name

        rng = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 10}}
        sym = {"name": "foo", "kind": 12, "range": rng, "selectionRange": rng}
        mock_lsp = self._mock_lsp(document_symbol=[sym])
        mock_lsp.open_file = AsyncMock()

        with patch.object(server, "lsp", mock_lsp):
            result = await server.list_file_symbols(tmp)

        self.assertIn("foo", result)
        self.assertIn("[Function]", result)

        import os; os.unlink(tmp)

    async def test_get_type_hierarchy_found(self):
        import server, tempfile
        with tempfile.NamedTemporaryFile(suffix=".h", delete=False, mode="w") as f:
            f.write("struct Animal {};\nstruct Dog : Animal {};\n")
            tmp = f.name

        sym = self._symbol("Dog", kind=23, file=tmp, line=1)
        root_item = self._call_item("Dog", file=tmp, line=1)
        root_item["kind"] = 23
        super_item = self._call_item("Animal", file=tmp, line=0)
        super_item["kind"] = 23

        mock_lsp = self._mock_lsp(
            workspace_symbol=[sym],
            prepare_type_hierarchy=[root_item],
            type_supertypes=[super_item],
            type_subtypes=[],
        )
        mock_lsp.open_file = AsyncMock()

        with patch.object(server, "lsp", mock_lsp):
            result = await server.get_type_hierarchy("Dog")

        self.assertIn("Dog", result)
        self.assertIn("Animal", result)
        self.assertIn("Supertypes", result)
        self.assertIn("Subtypes", result)

        import os; os.unlink(tmp)

    async def test_get_type_hierarchy_not_a_type(self):
        import server, tempfile
        with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False, mode="w") as f:
            f.write("void foo();\n")
            tmp = f.name

        sym = self._symbol("foo", file=tmp, line=0)
        mock_lsp = self._mock_lsp(workspace_symbol=[sym], prepare_type_hierarchy=[])
        mock_lsp.open_file = AsyncMock()

        with patch.object(server, "lsp", mock_lsp):
            result = await server.get_type_hierarchy("foo")

        self.assertIn("not a type", result)

        import os; os.unlink(tmp)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
