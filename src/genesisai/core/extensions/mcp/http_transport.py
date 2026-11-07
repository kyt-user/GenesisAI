"""MCP 2026-07-28 request-scoped HTTP JSON/SSE transport.

No protocol sessions, legacy handshake, GET resume, or ambiguous POST replay.
"""

from contextlib import nullcontext
import asyncio
import json

import httpx

from genesisai.shared.run_control import cancellation_requested, current_run
from genesisai.shared.security import ToolError
from .timeouts import RequestClock
from .protocol import PROTOCOL_VERSION, complete_result, encode_header, header_bindings, parameter_headers, request_params

MESSAGE_BYTES = 32 * 1024**2


class HttpTransport:
    def __init__(self, spec):
        self.spec = spec
        self.closed = False
        self.next_id = 0
        self.bindings = {}

    def _headers(self, method, params):
        headers = {**self.spec.headers, "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL_VERSION, "Mcp-Method": method}
        if method in {"tools/call", "resources/read", "prompts/get"}:
            headers["Mcp-Name"] = encode_header(params["uri" if method == "resources/read" else "name"])
        if method == "tools/call":
            headers.update(parameter_headers(self.bindings.get(params["name"], []), params.get("arguments", {})))
        return headers

    @staticmethod
    async def _messages(response, clock):
        def check():
            if cancellation_requested():
                raise ToolError("unknown_execution", "MCP 请求已取消，请核对远端结果")
            clock.remaining()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type == "application/json":
            body = bytearray()
            async for chunk in response.aiter_bytes():
                check()
                if len(body) + len(chunk) > MESSAGE_BYTES:
                    raise ToolError("mcp_message_limit", "MCP 单条响应超过32MiB")
                body.extend(chunk)
            yield {"message": json.loads(body), "id": None, "retry": None}
            return
        if content_type != "text/event-stream":
            raise ToolError("mcp_protocol", "MCP 响应必须为 application/json 或 text/event-stream")
        pending, fields, size = bytearray(), [], 0
        event_id, retry = None, None
        async for chunk in response.aiter_bytes():
            check()
            pending.extend(chunk)
            if len(pending) > MESSAGE_BYTES:
                raise ToolError("mcp_message_limit", "MCP 流行超过32MiB")
            while b"\n" in pending:
                raw, _, tail = pending.partition(b"\n")
                pending = bytearray(tail)
                line = raw.rstrip(b"\r").decode("utf-8")
                size += len(raw)
                if size > MESSAGE_BYTES:
                    raise ToolError("mcp_message_limit", "MCP 流事件超过32MiB")
                if not line:
                    data = "\n".join(fields)
                    if fields or event_id is not None or retry is not None:
                        yield {"message": json.loads(data) if data else None, "id": event_id, "retry": retry}
                    fields, size, event_id, retry = [], 0, None, None
                    continue
                if line.startswith(":"):
                    continue
                key, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if key == "data":
                    fields.append(value)
                elif key == "id" and "\0" not in value:
                    event_id = value
                elif key == "retry" and value.isascii() and value.isdigit():
                    retry = int(value) / 1000
        # A partial final event is never interpreted as a complete response.

    def request(self, method, params):
        if self.closed or cancellation_requested():
            raise ToolError("cancelled", "MCP 请求在发送前取消")
        self.next_id += 1
        identity = self.next_id
        clock = RequestClock(self.spec.request_timeouts)
        body = {"jsonrpc": "2.0", "id": identity, "method": method,
                "params": request_params(clock.params(params))}
        headers = self._headers(method, params)
        control = current_run.get()
        with asyncio.Runner() as runner:
            loop = runner.get_loop()

            async def perform():
                limits = clock.limits
                timeout = httpx.Timeout(connect=limits.connect, pool=limits.connect, read=None, write=limits.idle)
                async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=timeout) as client:
                    async with client.stream("POST", self.spec.url, headers=headers, json=body) as response:
                        if response.status_code not in {200, 400, 404}:
                            code = "unknown_execution" if response.status_code >= 500 else "mcp_http"
                            raise ToolError(code, f"MCP 请求失败，HTTP {response.status_code}；未自动重发")
                        async for event in self._messages(response, clock):
                            message = event["message"]
                            clock.observe(message)
                            if message is None:
                                continue
                            if isinstance(message, dict) and message.get("jsonrpc") == "2.0" and "method" in message and "id" not in message:
                                continue
                            result = complete_result(message, identity)
                            if response.status_code != 200:
                                raise ToolError("mcp_http", f"MCP HTTP {response.status_code} 未返回协议错误")
                            if method == "tools/list":
                                for tool in result.get("tools", []):
                                    try:
                                        self.bindings[tool["name"]] = header_bindings(tool["inputSchema"])
                                    except (ValueError, KeyError, TypeError):
                                        continue
                            return result
                raise ToolError("unknown_execution", "MCP 响应未完成；未重发调用")

            async def supervise():
                task = asyncio.create_task(perform())
                try:
                    while True:
                        left = clock.remaining()
                        done, _ = await asyncio.wait({task}, timeout=min(left, .1))
                        if done:
                            return task.result()
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

            task = loop.create_task(supervise())
            interrupt = control.interruptible(lambda: loop.call_soon_threadsafe(task.cancel)) if control else nullcontext()
            try:
                with interrupt:
                    return loop.run_until_complete(task)
            except asyncio.CancelledError as exc:
                raise ToolError("unknown_execution", "MCP 请求已取消；未重发调用，请核对远端结果") from exc
            except (ValueError, UnicodeError, httpx.HTTPError, RuntimeError) as exc:
                raise ToolError("unknown_execution", "MCP 响应中断或无法解析；未重发调用，请核对远端结果") from exc

    def close(self):
        self.closed = True
