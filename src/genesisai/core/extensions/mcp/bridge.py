"""MCP transport for explicitly registered server definitions.

MCP 工具以 ``category="mcp"`` 注册进同一 :class:`ToolRegistry`，与内置工具
走同一条 executor/permissions/ledger 管线。服务启动和外部调用分别授权。
"""

from __future__ import annotations

import json
import re
import subprocess
from contextlib import contextmanager
from threading import Event, Thread
import time

from genesisai.shared.time_limits import tool_deadline
from genesisai.shared.process_tree import owned_process, safe_environment
from dataclasses import asdict, dataclass, field
from pathlib import Path

from genesisai.core.tools.base import HostBoundTool, SchemaConfigError, ToolDescriptor, ToolSpec, object_schema
from genesisai.shared.security import ToolError, digest
from .protocol import PROTOCOL_VERSION, complete_result, header_bindings, request_params
from .timeouts import McpTimeouts, RequestClock
from genesisai.shared.run_control import current_run


RESOURCE_METHODS = {
    "list_mcp_resources": ("resources/list", "列出 MCP 服务资源，可按 cursor 翻页"),
    "list_mcp_resource_templates": ("resources/templates/list", "列出 MCP 资源 URI 模板，可按 cursor 翻页"),
    "read_mcp_resource": ("resources/read", "通过 MCP 服务读取指定资源 URI 的文字或图片"),
}
_TOOL_NAME = re.compile(r"[^a-z0-9_]")


class McpError(ValueError):
    """MCP 配置或协议错误。"""


def _candidate_links(structured):
    """Read explicit link fields anywhere in structured data, preserving provenance."""
    from urllib.parse import urlsplit
    candidates = {}
    pending = [(structured, '$', '')]
    while pending:
        value, path, inherited_title = pending.pop()
        if isinstance(value, list):
            pending.extend((child, f'{path}[{index}]', inherited_title) for index, child in reversed(list(enumerate(value))))
            continue
        if not isinstance(value, dict):
            continue
        title = value.get('title', value.get('name', inherited_title))
        title = title if isinstance(title, str) else ''
        for key, child in reversed(list(value.items())):
            child_path = path + '.' + key
            explicit = [child] if key in {'url', 'uri', 'href'} else (
                list(child.values()) if key == 'links' and isinstance(child, dict)
                else child if key == 'links' and isinstance(child, list) else [])
            for url in explicit:
                if not isinstance(url, str):
                    continue
                try:
                    parsed = urlsplit(url)
                    if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
                        continue
                except ValueError:
                    continue
                candidates.setdefault(url, {'url': url, 'title': title, 'field': child_path})
            if isinstance(child, (dict, list)):
                pending.append((child, child_path, title))
    return list(candidates.values())


@dataclass(frozen=True)
class McpServerSpec:
    name: str
    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict = field(default_factory=dict)
    read_only: bool = True
    scope: str = "project"
    cwd: str | None = None
    url: str | None = None
    headers: dict = field(default_factory=dict, repr=False)
    request_timeouts: dict = field(default_factory=lambda: McpTimeouts().as_dict())

    def __post_init__(self):
        from urllib.parse import urlsplit
        if not isinstance(self.name, str) or not self.name or self.transport not in {"stdio", "streamable_http"}:
            raise McpError("MCP 需要名称与明确的 stdio/streamable_http 传输类型")
        if self.transport == "stdio":
            if not isinstance(self.command, str) or not self.command or self.url or self.headers:
                raise McpError("stdio 需要 command，不能包含 url/headers")
        else:
            endpoint = urlsplit(self.url or "")
            if endpoint.scheme not in {"https", "http"} or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.fragment:
                raise McpError("远程 MCP URL 须为 HTTP(S)，不允许内嵌凭据或片段")
            if self.command or self.args or self.env or self.cwd:
                raise McpError("streamable_http 不接受 command/args/env/cwd")
            if (not isinstance(self.headers, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                    or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", k) or "\r" in v or "\n" in v
                    or k.casefold() in {"accept", "content-type", "content-length", "host", "mcp-session-id", "mcp-protocol-version", "last-event-id", "mcp-method", "mcp-name"}
                    or k.casefold().startswith("mcp-param-")
                    for k, v in self.headers.items())):
                raise McpError("MCP headers 无效或覆盖协议保留字段")
        try:
            object.__setattr__(self, 'request_timeouts', McpTimeouts.parse(self.request_timeouts).as_dict())
        except ValueError as exc:
            raise McpError(str(exc)) from exc
        if not isinstance(self.args, tuple) or any(not isinstance(arg, str) for arg in self.args):
            raise McpError("MCP args 必须是字符串元组")
        if not isinstance(self.env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in self.env.items()):
            raise McpError("MCP env 必须是字符串映射")
        if type(self.read_only) is not bool:
            raise McpError("MCP read_only 必须为布尔值")
        if self.scope not in {"user", "project"} or (self.cwd is not None and not isinstance(self.cwd, str)):
            raise McpError("MCP scope/cwd 无效")




class StdioTransport:
    """基于换行分隔 JSON-RPC 的 MCP stdio 传输。"""

    def __init__(self, spec: McpServerSpec):
        self.spec = spec
        env = {**safe_environment(), **spec.env}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            self._owned = owned_process(
                [spec.command, *spec.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=spec.cwd,
            )
            self.process = self._owned.__enter__()
        except OSError as exc:
            raise McpError(f"MCP server 无法启动：{exc}") from exc
        self._next_id = 0

    def _write(self, message: dict) -> None:
        if self.process.stdin is None:
            raise McpError("MCP server 已关闭连接")
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    @contextmanager
    def _bounded_io(self):
        clock = RequestClock(self.spec.request_timeouts)
        stopped = Event()
        failure = []
        control = current_run.get()
        def watch():
            while not stopped.wait(.05):
                try:
                    clock.remaining()
                    if control and control.cancelled.is_set():
                        raise ToolError('unknown_execution', 'MCP 请求已取消；未重发调用')
                except ToolError as exc:
                    failure.append(exc)
                    self.close()
                    return
        monitor = Thread(target=watch, daemon=True)
        monitor.start()
        try:
            yield clock
        finally:
            stopped.set()
            monitor.join()
            if failure:
                raise failure[0]

    def request(self, method: str, params: dict) -> dict:
        with self._bounded_io() as clock:
            self._next_id += 1
            self._write({"jsonrpc": "2.0", "id": self._next_id, "method": method,
                         "params": request_params(clock.params(params))})
            while True:
                line = self.process.stdout.readline(32 * 1024 * 1024 + 1)
                if not line or len(line) > 32 * 1024 * 1024 or not line.endswith(b"\n"):
                    raise McpError("MCP 连接关闭或响应超过大小上限")
                response = json.loads(line)
                if not isinstance(response, dict):
                    raise McpError("无效的 MCP 消息")
                clock.observe(response)
                if "method" in response:
                    if "id" in response:
                        raise McpError("当前 MCP 协议不允许服务端独立 JSON-RPC 请求")
                    continue
                return complete_result(response, self._next_id)

    def notify(self, method: str, params: dict) -> None:
        with self._bounded_io():
            self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> None:
        owner, self._owned = self._owned, None
        if owner:
            owner.__exit__(None, None, None)


class ConnectMcp(HostBoundTool):
    def prepare(self, context, args):
        return self.host_service.launch_preview(args["server"])

    def execute(self, context, args):
        return _submit_job(context, self.host_service, args['server'], 'server/discover', {})


class McpToolImplementation(HostBoundTool):
    """把 MCP ``tools/call`` 包装成标准工具实现契约。"""

    def __init__(self, bridge: "McpBridge", server: str, tool: str):
        super().__init__(bridge)
        self.server = server
        self.tool = tool

    def prepare(self, context, args):
        return {**self.host_service.launch_preview(self.server), "target": f"{self.server}/{self.tool}", "args": args}

    def execute(self, context, args):
        return _submit_job(context, self.host_service, self.server, 'tools/call', {'name': self.tool, 'arguments': args})


class ResourceTool(HostBoundTool):
    def __init__(self, bridge, method):
        super().__init__(bridge)
        self.method = method

    def prepare(self, context, args):
        return {**self.host_service.launch_preview(args["server"]), "method": self.method, "args": args}

    def execute(self, context, args):
        return _submit_job(context, self.host_service, args['server'], self.method, {k: v for k, v in args.items() if k != 'server'})


def _submit_job(context, bridge, server, method, params):
    snapshot = bridge.job_snapshot(server)
    job = context.runtime.jobs.start_operation('mcp_request', {**snapshot, 'method': method, 'params': params})
    state = context.runtime.jobs.wait(job['job_id'], 5, cancel_on_interrupt=True)
    if state['status'] not in {'queued', 'running', 'stopping'}:
        return bridge.job_result(context.runtime.jobs.operation_result(job['job_id']))
    return {**state, 'server': server, 'continuation': '使用 read_job(job_id) 获取最终结果；不要重发原始请求'}


class McpBridge:
    """连接配置的 MCP server，注册其工具并转发调用。"""

    def __init__(self, workspace, registry, loader, *, transport_factory=None):
        self.workspace = Path(workspace).resolve()
        self.registry = registry
        self.loader = loader
        self.transport_factory = transport_factory or self._transport
        self.custom_transport_factory = transport_factory
        self.servers: list[McpServerSpec] = []
        self.transports: dict[str, object] = {}
        self.diagnostics: list[str] = []
        self.config: dict[str, McpServerSpec] = {}
        self.tool_owners: dict[str, tuple[str, str]] = {}

    def job_snapshot(self, server):
        from genesisai.core.jobs.operations import portable_adapter
        if server not in self.config:
            raise ToolError('mcp_unavailable', 'MCP 服务配置已移除')
        return {'server': asdict(self.config[server]),
                'transport_factory': portable_adapter(self.custom_transport_factory) if self.custom_transport_factory else None}

    def job_result(self, envelope):
        server, method, result = envelope['server'], envelope['method'], envelope['response']
        if method == 'server/discover':
            spec = self.config.get(server)
            if spec is None or digest(asdict(spec)) != envelope['config_digest']:
                raise ToolError('mcp_unavailable', '目录发现期间 MCP 配置已变化，请按新配置重新发现')
            self.cache[envelope['config_digest']] = result
            self._register_tools(spec, result['tools'])
            if result['resources']:
                self._register_resources()
            return {'server': server, 'resources': result['resources'],
                    'tools': [self._tool_name(server, tool['name']) for tool in result['tools']]}
        if method != 'tools/call':
            return self._resource_result(server, method, result)
        return self._tool_result(server, envelope['selector'], result)

    @staticmethod
    def _transport(spec):
        if spec.transport == "stdio":
            return StdioTransport(spec)
        from genesisai.core.extensions.mcp.http_transport import HttpTransport
        return HttpTransport(spec)

    def configure(self, cache: dict, *, servers) -> "McpBridge":
        self.cache = cache
        servers = list(servers)
        if any(not isinstance(spec, McpServerSpec) for spec in servers) or len({spec.name for spec in servers}) != len(servers):
            raise McpError("MCP 服务定义无效或名称重复")
        selected = {spec.name: spec for spec in servers}
        for server, previous in self.config.items():
            if selected.get(server) != previous:
                transport = self.transports.pop(server, None)
                if transport:
                    transport.close()
                self.servers = [item for item in self.servers if item.name != server]
                for name, owner in list(self.tool_owners.items()):
                    if owner[0] == server:
                        self.registry.unregister_dynamic(name)
                        self.loader.unregister_dynamic(name)
                        del self.tool_owners[name]
        self.config = selected
        if self.config:
            name = "connect_mcp"
            spec = ToolSpec(parameters=object_schema(["server"], server={"type": "string", "enum": list(self.config)}),
                            permission="network", network=True, side_effect=True, routes=("mcp",),
                            timeout_seconds=30)
            self.registry.register(ToolDescriptor(name, True, "mcp", "启动指定 MCP 服务并发现工具", self.workspace, spec))
            self.loader.register(name, ConnectMcp(self))
        else:
            self.registry.unregister_dynamic("connect_mcp")
            self.loader.unregister_dynamic("connect_mcp")
        for spec in self.config.values():
            discovery = cache.get(digest(asdict(spec)), {})
            try:
                self._register_tools(spec, discovery.get("tools", []))
            except McpError as exc:
                self.diagnostics.append(str(exc))
        if any(cache.get(digest(asdict(spec)), {}).get("resources") for spec in self.config.values()):
            self._register_resources()
        else:
            for name in RESOURCE_METHODS:
                self.registry.unregister_dynamic(name)
                self.loader.unregister_dynamic(name)
        return self

    def launch_preview(self, server):
        spec = self.config.get(server)
        if spec is None:
            raise McpError("未知 MCP 服务")
        return {"target": server, "transport": spec.transport, "endpoint": spec.url if spec.transport == "streamable_http" else [spec.command, *spec.args], "config_digest": digest(asdict(spec))}

    def connect(self, server):
        self.ensure_connected(server)
        discovery = self.cache[digest(asdict(self.config[server]))]
        return {"server": server, "resources": discovery["resources"],
                "tools": [self._tool_name(server, tool["name"]) for tool in discovery["tools"]]}

    def ensure_connected(self, server):
        if server not in self.transports:
            self._connect(self.config[server])

    def _connect(self, spec: McpServerSpec) -> None:
        transport = self.transport_factory(spec)
        try:
            discovery = transport.request("server/discover", {})
            if not isinstance(discovery, dict) or PROTOCOL_VERSION not in discovery.get("supportedVersions", []):
                raise McpError("MCP 服务不支持当前协议版本 " + PROTOCOL_VERSION)
            capabilities = discovery.get("capabilities", {})
            resources = "resources" in capabilities
            tools = []
            if "tools" in capabilities:
                params, seen = {}, set()
                while True:
                    listing = transport.request("tools/list", params)
                    if not isinstance(listing, dict) or not isinstance(listing.get("tools"), list):
                        raise McpError("MCP 工具目录无效")
                    tools.extend(listing["tools"])
                    cursor = listing.get("nextCursor")
                    if not cursor:
                        break
                    if not isinstance(cursor, str) or cursor in seen:
                        raise McpError("MCP 工具目录分页异常")
                    seen.add(cursor)
                    params = {"cursor": cursor}
            self._register_tools(spec, tools)
            tools = [tool for tool in tools if (spec.name, tool["name"]) in self.tool_owners.values()]
        except BaseException:
            transport.close()
            raise
        self.cache[digest(asdict(spec))] = {"tools": tools, "resources": resources}
        if resources:
            self._register_resources()
        self.servers.append(spec)
        self.transports[spec.name] = transport
        if not tools and not resources:
            self.diagnostics.append(f"{spec.name}: 未发现任何工具")

    def _register_tools(self, spec, tools):
        # Validate the whole server response before publishing any callable names.
        pending = {}
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]:
                raise McpError(f"{spec.name}: MCP 工具名称无效")
            if spec.transport == "streamable_http":
                try:
                    header_bindings(tool["inputSchema"])
                except (KeyError, ValueError, TypeError) as exc:
                    self.diagnostics.append(f"{spec.name}/{tool['name']}: HTTP 参数头声明无效：{exc}")
                    continue
            try:
                descriptor = self._descriptor(spec, tool)
            except (KeyError, ValueError, SchemaConfigError) as exc:
                raise McpError(f"{spec.name}/{tool['name']}: inputSchema 无效") from exc
            name = descriptor.name
            owner = (spec.name, tool["name"])
            if name in pending or (name in self.registry and self.tool_owners.get(name) != owner):
                raise McpError(f"{spec.name}: MCP 工具名称冲突：{name}")
            pending[name] = (owner, descriptor)
        for name, owner in list(self.tool_owners.items()):
            if owner[0] == spec.name and name not in pending:
                self.registry.unregister_dynamic(name)
                self.loader.unregister_dynamic(name)
                del self.tool_owners[name]
        for name, (owner, descriptor) in pending.items():
            self.registry.register(descriptor)
            self.loader.register(name, McpToolImplementation(self, *owner))
            self.tool_owners[name] = owner

    def _descriptor(self, spec: McpServerSpec, tool: dict) -> ToolDescriptor:
        name = self._tool_name(spec.name, tool["name"])
        description = tool.get("description")
        if not isinstance(description, str) or not description.strip():
            description = f"MCP 工具 {tool['name']}（{spec.name}）"
        tool_spec = ToolSpec(
            parameters=tool["inputSchema"], external_schema=True,
            permission="network", network=True,
            path_scope="none",
            side_effect=not spec.read_only, timeout_seconds=30,
        )
        tool_spec.validate()
        return ToolDescriptor(name, True, "mcp", description.strip(), self.workspace, tool_spec,
                              source="external", scope=spec.scope, provider=spec.name)

    @staticmethod
    def _tool_name(server: str, tool: str) -> str:
        raw = f"mcp_{server}_{tool}"
        name = _TOOL_NAME.sub("_", raw.lower())
        if not name or not name[0].isalpha():
            name = "mcp_" + name
        return name if name == raw and len(name) <= 63 else name[:50] + "_" + digest([server, tool])[:12]

    def _register_resources(self):
        servers = sorted(name for name, spec in self.config.items()
                         if self.cache.get(digest(asdict(spec)), {}).get("resources"))
        for name, (method, description) in RESOURCE_METHODS.items():
            required = ["server"]
            props = {"server": {"type": "string", "enum": servers}}
            if method == "resources/read":
                required.append("uri")
                props["uri"] = {"type": "string", "minLength": 1}
            else:
                props["cursor"] = {"type": "string"}
            spec = ToolSpec(parameters=object_schema(required, **props), permission="network", network=True,
                            routes=("mcp",), timeout_seconds=30)
            self.registry.register(ToolDescriptor(name, True, "mcp", description, self.workspace, spec))
            self.loader.register(name, ResourceTool(self, method))

    def resource(self, server, method, params):
        self.ensure_connected(server)
        if not self.cache[digest(asdict(self.config[server]))]["resources"]:
            raise ToolError("unsupported_resource", "此服务未声明资源能力")
        result = self._request(server, method, params)
        return self._resource_result(server, method, result)

    def _resource_result(self, server, method, result):
        if method != "resources/read":
            key = "resources" if method == "resources/list" else "resourceTemplates"
            if not isinstance(result.get(key), list):
                raise ToolError("mcp_error", "无效的资源目录")
            return {"server": server, **result}
        blocks, uris = [], []
        for entry in result["contents"]:
            uris.append(entry["uri"])
            if isinstance(entry.get("text"), str):
                blocks.append({"type": "text", "text": entry["text"]})
            elif isinstance(entry.get("blob"), str) and entry.get("mimeType", "").startswith("image/"):
                blocks.append({"type": "image", "data": entry["blob"], "mimeType": entry["mimeType"]})
            else:
                raise ToolError("unsupported_content", "当前仅支持文字或图片资源")
        return {**self._content(server, method, {"content": blocks}), "uris": uris}

    def invoke(self, server, tool, args):
        self.ensure_connected(server)
        # A restored catalog may predate the server that was just launched.
        discovery = self.cache[digest(asdict(self.config[server]))]
        if not any(item["name"] == tool for item in discovery["tools"]):
            raise ToolError("mcp_unavailable", "此服务已移除该工具")
        self.registry.get(self._tool_name(server, tool)).validate_arguments(args)
        return self.call(server, tool, args)

    def call(self, server: str, tool: str, args: dict) -> dict:
        result = self._request(server, "tools/call", {"name": tool, "arguments": args})
        return self._tool_result(server, tool, result)

    def _tool_result(self, server, tool, result):
        if result.get("isError"):
            message = "\n".join(str(b.get("text", "")) for b in result.get("content", []) if isinstance(b, dict))
            raise ToolError("mcp_tool_error", message[:2000] or "MCP 工具报告失败")
        return self._content(server, tool, result)

    def _request(self, server, method, params):
        transport = self.transports.get(server)
        if transport is None:
            raise ToolError("mcp_unavailable", f"MCP server 未连接：{server}")
        try:
            result = transport.request(method, params)
            if not isinstance(result, dict):
                raise McpError("MCP 服务返回无效结果")
            return result
        except (ToolError, ValueError, OSError, KeyError) as exc:
            self.transports.pop(server, None)
            self.servers = [entry for entry in self.servers if entry.name != server]
            transport.close()
            if isinstance(exc, ToolError):
                raise
            raise ToolError("mcp_error", str(exc)) from exc

    def _content(self, server, tool, result):
        if not isinstance(result, dict):
            raise ToolError("mcp_error", "MCP 工具返回无效结果")
        blocks = result.get('content', [])
        if not isinstance(blocks, list) or any(not isinstance(b, dict) for b in blocks):
            raise ToolError('mcp_error', 'MCP 内容块无效')
        unsupported = {str(b.get('type')) for b in blocks if b.get('type') not in {'text', 'image', 'resource_link'}}
        if unsupported:
            raise ToolError('unsupported_content', '当前仅接入文字和图片，MCP 返回了未支持的内容：' + ', '.join(sorted(unsupported)))
        images = []
        image_blocks = [b for b in blocks if b.get('type') == 'image']
        if image_blocks:
            handler = getattr(self, 'image_handler', None)
            if handler is None:
                raise ToolError('image_unavailable', 'MCP 图片服务未绑定')
            config = self.image_config()
            raw_bytes = sum(len(block.get('data', '')) * 3 // 4 for block in image_blocks if isinstance(block.get('data'), str))
            if raw_bytes > config.max_pending_image_bytes:
                raise ToolError('image_limit', 'MCP 图片超过输入资源配额')
            retained = 0
            for block in image_blocks:
                image = handler(block)
                retained += image['byte_size'] + len(block.get('data', '')) * 3 // 4
                if retained > config.max_pending_image_bytes:
                    raise ToolError('image_limit', 'MCP 原图与预览超过输入资源配额，已有快照已保留')
                images.append(image)
        texts = [
            item.get("text", "")
            for item in result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        ]
        payload = {
            "server": server,
            "tool": tool,
            "text": "\n".join(texts),
            "is_error": bool(result.get("isError")),
        }
        if "structuredContent" in result:
            payload["structuredContent"] = result["structuredContent"]
        resource_links = [block for block in blocks if block.get('type') == 'resource_link']
        if resource_links:
            payload['resource_links'] = resource_links
        links = _candidate_links({'structuredContent': result.get('structuredContent'), 'content': resource_links})
        if links:
            payload['candidate_links'] = links
        if images:
            payload.update(images=images, source_refs=[p['image_ref'] for p in images])
        return payload

    def close(self) -> None:
        for transport in self.transports.values():
            transport.close()
        self.transports.clear()
        self.servers.clear()
