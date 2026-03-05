"""Tests for mcp-clangd.

Runs without a real clangd binary by driving the LSP client with a fake
in-process server that echoes back canned LSP responses.

Run with:
    uv run pytest tests.py -v
or:
    uv run python tests.py
"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from lsp_client import LSPClient, path_to_uri, uri_to_path, SYMBOL_KINDS


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


# ---------------------------------------------------------------------------
# Integration-style tests: MCP tool handlers
# ---------------------------------------------------------------------------

class TestMCPTools(unittest.IsolatedAsyncioTestCase):
    """Test tool handler logic by patching the global lsp object in main."""

    def _mock_lsp(self, **method_results):
        mock = AsyncMock(spec=LSPClient)
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


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
