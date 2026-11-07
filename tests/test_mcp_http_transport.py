import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from genesisai.core.extensions.mcp.http_transport import HttpTransport
from genesisai.shared.security import ToolError
from genesisai.core.extensions.mcp.protocol import encode_header, header_bindings, parameter_headers
from test_capability_lifecycle import capability_runtime, created, invoke, reference, accept


@pytest.fixture
def mcp_http_server(monkeypatch):
    state = {"sessions": 0, "posts": [], "headers": [], "gets": [], "deleted": 0, "mode": "json", "expire": False, "stream_started": Event()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body=b"", *, content_type="application/json", headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["posts"].append(message)
            state["headers"].append(dict(self.headers))
            method = message.get("method")
            if "id" not in message:
                self.send(202)
                return
            if method == "server/discover":
                state["sessions"] += 1
                result = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}
                self.send(200, json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode(), headers={"MCP-Session-Id": f"session-{state['sessions']}"})
                return
            if state["expire"]:
                state["expire"] = False
                self.send(404, json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Unknown method"}}).encode())
                return
            result = {"tools": state.get("tools", [])} if method == "tools/list" else {"content": [{"type": "text", "text": "真实结果"}]}
            result["resultType"] = "complete"
            payload = {"jsonrpc": "2.0", "id": message["id"], "result": result}
            state["pending"] = payload
            if state["mode"] in {'progress', 'heartbeat', 'repeat_progress'}:
                import time
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                try:
                    for index in range(8):
                        if state["mode"] == 'heartbeat':
                            body = b': alive\n\n'
                        else:
                            value = index if state["mode"] == 'progress' else 0
                            event = {'jsonrpc': '2.0', 'method': 'notifications/progress',
                                     'params': {'progressToken': message['params']['_meta']['progressToken'], 'progress': value}}
                            body = ('data: ' + json.dumps(event) + '\n\n').encode()
                        self.wfile.write(body); self.wfile.flush()
                        state["stream_started"].set()
                        time.sleep(.08)
                    self.wfile.write(('data: ' + json.dumps(payload) + '\n\n').encode())
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                return
            if state["mode"] == "resume":
                self.send(200, b"id: first-event\nretry: 1\ndata:\n\n", content_type="text/event-stream")
            elif state["mode"] == "drop":
                self.send(200, b": disconnected\n\n", content_type="text/event-stream")
            elif state["mode"] == "redirect":
                self.send(307, headers={"Location": "/must-not-follow"})
            elif state["mode"] == "sse":
                body = ("event: message\nid: complete\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()
                self.send(200, body, content_type="text/event-stream; charset=utf-8")
            else:
                self.send(200, json.dumps(payload).encode())

        def do_GET(self):
            state["gets"].append(dict(self.headers))
            body = ("id: final-event\ndata: " + json.dumps(state["pending"], ensure_ascii=False) + "\n\n").encode()
            self.send(200, body, content_type="text/event-stream")

        def do_DELETE(self):
            state["deleted"] += 1
            self.send(204)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    import socket
    import _socket
    def local_only(sock, address):
        import sys
        # Windows asyncio builds its wakeup socketpair with a loopback connect.
        internal_pair = sys._getframe(1).f_code is getattr(socket.socketpair, '__code__', None)
        assert address == ("127.0.0.1", server.server_port) or (internal_pair and address[0] == '127.0.0.1'), "Only fixture HTTP and asyncio wakeup sockets are permitted"
        return _socket.socket.connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", local_only)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    spec = SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}/mcp", headers={"Authorization": "Bearer test-only"}, request_timeouts={"first_response": 5, "idle": 5})
    try:
        yield spec, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def connected(spec):
    transport = HttpTransport(spec)
    result = transport.request("server/discover", {})
    assert result["supportedVersions"] == ["2026-07-28"]
    return transport


@pytest.mark.parametrize("mode", ["json", "sse"])
def test_real_http_messages_per_request_metadata(mcp_http_server, mode):
    spec, state = mcp_http_server
    transport = connected(spec)
    state["mode"] = mode
    try:
        result = transport.request("tools/call", {"name": "echo", "arguments": {}})
        assert result["content"][0]["text"] == "真实结果"
        assert len([m for m in state["posts"] if m.get("method") == "tools/call"]) == 1
        assert "MCP-Session-Id" not in state["headers"][-1]
        assert state["headers"][-1]["Mcp-Method"] == "tools/call"
        assert state["headers"][-1]["Mcp-Name"] == "echo"
        assert state["posts"][-1]["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        assert state["headers"][-1]["MCP-Protocol-Version"] == "2026-07-28"
        assert state["headers"][-1]["Authorization"] == "Bearer test-only"
        assert not state["gets"]
    finally:
        transport.close()
    assert state["deleted"] == 0


@pytest.mark.parametrize("mode", ["drop", "resume"])
def test_incomplete_stream_never_replays_post_or_get(mcp_http_server, mode):
    spec, state = mcp_http_server
    transport = connected(spec)
    state["mode"] = mode
    try:
        with pytest.raises(ToolError) as caught:
            transport.request("tools/call", {"name": "mutate", "arguments": {}})
        assert caught.value.error_type == "unknown_execution"
        assert len([m for m in state["posts"] if m.get("method") == "tools/call"]) == 1
        assert not state["gets"]
    finally:
        transport.close()


def test_method_not_found_never_reinitializes(mcp_http_server):
    spec, state = mcp_http_server
    transport = connected(spec)
    state["expire"] = True
    try:
        with pytest.raises(ToolError, match="-32601"):
            transport.request("tools/list", {})
        assert state["sessions"] == 1
        assert len(state["posts"]) == 2
    finally:
        transport.close()


def test_redirect_cannot_forward_credentials(mcp_http_server):
    spec, state = mcp_http_server
    transport = connected(spec)
    state["mode"] = "redirect"
    try:
        with pytest.raises(ToolError, match="307"):
            transport.request("tools/list", {})
        assert len([m for m in state["posts"] if m.get("method") == "tools/list"]) == 1
        assert not state["gets"]
    finally:
        transport.close()


def test_remote_capability_tests_registers_and_uses_the_declared_timeout(capability_runtime, mcp_http_server, monkeypatch):
    runtime = capability_runtime
    spec, state = mcp_http_server
    state["tools"] = [{"name": "echo", "description": "Echo a value", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]
    monkeypatch.setenv("GENESIS_MCP_TEST_HEADER", "Bearer isolated-test")
    package = {"kind": "mcp", "name": "http_echo", "description": "Remote MCP fixture", "scope": "project", "files": {},
               "definition": {"transport": "streamable_http", "url": spec.url, "read_only": False,
                              "headers_from": {"Authorization": "GENESIS_MCP_TEST_HEADER"}, "request_timeouts": {"first_response": 602, "idle": 90}},
               "tests": [{"name": "echo", "input": {"method": "tools/call", "params": {"name": "echo", "arguments": {}}},
                          "expect": {"type": "object", "properties": {"text": {"const": "真实结果"}}, "required": ["text"]}}]}
    item = created(runtime, package)
    _, tested = invoke(runtime, "test_capability", reference(item))
    assert tested["data"]["report"]["passed"], tested
    assert "Bearer isolated-test" not in json.dumps(runtime.capabilities.inspect(item["id"]))
    accept(runtime, item)
    _, discovery = invoke(runtime, "connect_mcp", {"server": "http_echo"})
    assert discovery["ok"], discovery
    assert runtime.mcp.config["http_echo"].request_timeouts["first_response"] == 602
    assert runtime.mcp.config["http_echo"].request_timeouts["idle"] == 90
    assert runtime.mcp.config["http_echo"].request_timeouts["total"] is None
    state["mode"] = "sse"
    _, result = invoke(runtime, "mcp_http_echo_echo", {})
    assert result["data"]["text"] == "真实结果"
    state["mode"] = "drop"
    call, result = invoke(runtime, "mcp_http_echo_echo", {})
    assert result["error"]["code"] == "unknown_execution"
    assert runtime.store.record.calls[call["id"]]["state"] == "unknown"
    count = len(state["posts"])
    invoke(runtime, "mcp_http_echo_echo", {}, call=call)
    assert len(state["posts"]) == count


def test_http_parameter_headers_use_exact_nested_path_and_safe_encoding(mcp_http_server):
    spec, state = mcp_http_server
    schema = {"type": "object", "properties": {
        "routing": {"type": "object", "properties": {
            "region": {"type": "string", "x-mcp-header": "Region"},
            "enabled": {"type": "boolean", "x-mcp-header": "Enabled"},
            "id": {"type": "integer", "x-mcp-header": "Id"}}}}}
    state["tools"] = [{"name": "查询", "inputSchema": schema}]
    transport = connected(spec)
    try:
        transport.request("tools/list", {})
        transport.request("tools/call", {"name": "查询", "arguments": {"routing": {"region": " 亚洲\n", "enabled": False, "id": 42}}})
        headers = state["headers"][-1]
        assert headers["Mcp-Name"] == encode_header("查询")
        assert headers["Mcp-Param-Region"] == encode_header(" 亚洲\n")
        assert headers["Mcp-Param-Enabled"] == "false"
        assert headers["Mcp-Param-Id"] == "42"
        bindings = header_bindings(schema)
        assert parameter_headers(bindings, {"routing": {"region": None}}) == {}
        with pytest.raises(ToolError):
            parameter_headers(bindings, {"routing": {"id": 2**53}})
        assert encode_header("=?base64?literal?=") != "=?base64?literal?="
    finally:
        transport.close()


@pytest.mark.parametrize("schema", [
    {"type": "object", "properties": {"x": {"type": "number", "x-mcp-header": "X"}}},
    {"type": "object", "anyOf": [{"properties": {"x": {"type": "string", "x-mcp-header": "X"}}}]},
    {"type": "object", "properties": {"x": {"type": "string", "x-mcp-header": "X"}, "y": {"type": "string", "x-mcp-header": "x"}}},
    {"type": "object", "properties": {"x": {"type": "string", "x-mcp-header": "x\r\ny"}}},
])
def test_bad_header_annotation_excludes_only_that_tool(capability_runtime, mcp_http_server, schema):
    from genesisai.core.extensions.mcp.bridge import McpBridge, McpServerSpec
    runtime = capability_runtime
    spec, state = mcp_http_server
    state["tools"] = [{"name": "valid", "inputSchema": {"type": "object"}}, {"name": "invalid", "inputSchema": schema}]
    bridge = McpBridge(runtime.store.workspace, runtime.registry, runtime.loader)
    bridge.configure({}, servers=[McpServerSpec("headers", "streamable_http", url=spec.url)])
    try:
        result = bridge.connect("headers")
        assert result["tools"] == ["mcp_headers_valid"]
        assert len(bridge.diagnostics) == 1
        assert "invalid" in bridge.diagnostics[0]
    finally:
        bridge.close()


@pytest.mark.parametrize('mode,total,success', [
    ('progress', None, True), ('progress', .25, False),
    ('heartbeat', None, False), ('repeat_progress', None, False),
])
def test_semantic_progress_timeouts_do_not_replay(mcp_http_server, mode, total, success):
    import time
    spec, state = mcp_http_server
    transport = connected(spec)
    spec.request_timeouts = {'first_response': .3, 'idle': .3, 'total': total}
    state['mode'] = mode
    started = time.monotonic()
    try:
        if success:
            assert transport.request('tools/call', {'name': 'long', 'arguments': {}})['resultType'] == 'complete'
            assert time.monotonic() - started > .5
        else:
            with pytest.raises(ToolError, match='超时'):
                transport.request('tools/call', {'name': 'long', 'arguments': {}})
            assert time.monotonic() - started < 1.5
        assert len([m for m in state['posts'] if m['method'] == 'tools/call']) == 1
    finally:
        transport.close()


def test_http_cancel_interrupts_real_stream(mcp_http_server):
    import time
    from genesisai.shared.run_control import RunControl
    spec, state = mcp_http_server
    transport = connected(spec)
    state['mode'] = 'progress'
    control = RunControl(lambda *a: None)
    cancelled_at = []
    def cancel_stream():
        state["stream_started"].wait(5)
        cancelled_at.append(time.monotonic())
        control.cancel()
    canceller = Thread(target=cancel_stream)
    canceller.start()
    try:
        with pytest.raises(ToolError, match='取消'):
            control.execute(lambda: transport.request('tools/call', {'name': 'long', 'arguments': {}}))
        assert state["stream_started"].is_set(), 'Cancellation must target an actual streaming response'
        assert cancelled_at and time.monotonic() - cancelled_at[0] < 1.5
        assert len([m for m in state['posts'] if m['method'] == 'tools/call']) == 1
    finally:
        canceller.join()
        transport.close()
