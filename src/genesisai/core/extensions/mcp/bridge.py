"""MCP 桥接缝：读取 ``projects/<project_id>/mcp.json``，连接 MCP server 并动态注册工具。

MCP 工具以 ``category="mcp"`` 注册进同一 :class:`ToolRegistry`，与内置工具
走同一条 executor/permissions/ledger 管线。服务启动和外部调用分别授权。
"""

from __future__ import annotations

import json
import re
import subprocess
from itertools import islice
from contextlib import contextmanager
from threading import Timer
import time

from genesisai.shared.time_limits import tool_deadline
from genesisai.shared.process_tree import owned_process, safe_environment
from dataclasses import asdict, dataclass, field
from pathlib import Path

from genesisai.core.tools.base import HostBoundTool, SchemaConfigError, ToolDescriptor, ToolSpec, object_schema
from genesisai.shared.security import ToolError, digest


MCP_CONFIG = Path("mcp.json")
PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "genesisai", "version": "0.1.0"}
RESOURCE_METHODS = {
    "list_mcp_resources": ("resources/list", "列出 MCP 服务资源，可按 cursor 翻页"),
    "list_mcp_resource_templates": ("resources/templates/list", "列出 MCP 资源 URI 模板，可按 cursor 翻页"),
    "read_mcp_resource": ("resources/read", "通过 MCP 服务读取指定资源 URI 的文字或图片"),
}
_TOOL_NAME = re.compile(r"[^a-z0-9_]")


class McpError(ValueError):
    """MCP 配置或协议错误。"""


def _candidate_links(structured):
    """Extract explicit item links, never URLs embedded in prose or tool instructions."""
    items = structured.get("items") if isinstance(structured, dict) else None
    if not isinstance(items, list):
        return []
    candidates = {}
    for item in items[:40]:
        if not isinstance(item, dict):
            continue
        links = item.get("links")
        urls = [item.get("url")]
        if isinstance(links, dict):
            urls.extend(islice(links.values(), 8))
        for url in urls:
            if isinstance(url, str) and len(url) <= 2000 and url.startswith(("https://", "http://")):
                title = item.get("title")
                candidates[url] = {"url": url, "title": title[:100] if isinstance(title, str) else ""}
                if len(candidates) == 40:
                    return list(candidates.values())
    return list(candidates.values())


@dataclass(frozen=True)
class McpServerSpec:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict = field(default_factory=dict)
    read_only: bool = True


def load_servers(workspace) -> list[McpServerSpec]:
    """读取应用管理的项目目录 ``projects/<project_id>/mcp.json``；缺失时返回空列表。"""
    path = Path(workspace).resolve() / MCP_CONFIG
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise McpError(f"mcp.json 无法读取：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("servers"), dict):
        raise McpError("mcp.json 必须包含 servers 对象")
    servers = []
    for name, cfg in data["servers"].items():
        if not isinstance(name, str) or not isinstance(cfg, dict):
            raise McpError("MCP server 配置无效")
        command = cfg.get("command")
        if not isinstance(command, str) or not command:
            raise McpError(f"MCP server {name} 缺少 command")
        args = cfg.get("args", [])
        if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
            raise McpError(f"MCP server {name} args 无效")
        env = cfg.get("env", {})
        if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
            raise McpError(f"MCP server {name} env 无效")
        read_only = cfg.get("read_only", True)
        if type(read_only) is not bool:
            raise McpError(f"MCP server {name} read_only 必须是布尔值")
        servers.append(
            McpServerSpec(
                name=name,
                command=command,
                args=tuple(args),
                env=dict(env),
                read_only=read_only,
            )
        )
    return servers


class StdioTransport:
    """基于换行分隔 JSON-RPC 的 MCP stdio 传输。"""

    def __init__(self, spec: McpServerSpec):
        env = {**safe_environment(), **spec.env}
        try:
            self._owned = owned_process(
                [spec.command, *spec.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                env=env,
            )
            self.process = self._owned.__enter__()
        except OSError as exc:
            raise McpError(f"MCP server 无法启动：{exc}") from exc
        self._next_id = 0

    def _write(self, message: dict) -> None:
        if self.process.stdin is None:
            raise McpError("MCP server 已关闭连接")
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    @contextmanager
    def _bounded_io(self):
        deadline = tool_deadline.get()
        timer = Timer(max(0, deadline - time.monotonic()), self.close) if deadline else None
        if timer:
            timer.start()
        try:
            yield
        finally:
            if timer:
                timer.cancel()
                timer.join()
                if time.monotonic() >= deadline:
                    raise ToolError("tool_timeout", "MCP 响应超过工具时限，已关闭本地服务")

    def request(self, method: str, params: dict) -> dict:
        with self._bounded_io():
            return self._request(method, params)

    def _request(self, method, params):
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        while True:
            line = self.process.stdout.readline(32 * 1024 * 1024 + 1)
            if not line or not line.endswith("\n"):
                raise McpError("MCP 连接关闭或响应超过大小上限")
            response = json.loads(line)
            if not isinstance(response, dict):
                raise McpError("无效的 MCP 消息")
            if "method" in response:
                if "id" in response:
                    reply = {"result": {}} if response["method"] == "ping" else {
                        "error": {"code": -32601, "message": "Unsupported client method"}}
                    self._write({"jsonrpc": "2.0", "id": response["id"], **reply})
                continue
            if response.get("id") != self._next_id:
                raise McpError("MCP 响应 ID 不匹配")
            if "error" in response:
                raise McpError(f"MCP 调用失败：{response['error']}")
            return response["result"]

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
        return self.host_service.connect(args["server"])


class McpToolImplementation(HostBoundTool):
    """把 MCP ``tools/call`` 包装成标准工具实现契约。"""

    def __init__(self, bridge: "McpBridge", server: str, tool: str):
        super().__init__(bridge)
        self.server = server
        self.tool = tool

    def prepare(self, context, args):
        return {**self.host_service.launch_preview(self.server), "target": f"{self.server}/{self.tool}", "args": args}

    def execute(self, context, args):
        return self.host_service.invoke(self.server, self.tool, args)


class ResourceTool(HostBoundTool):
    def __init__(self, bridge, method):
        super().__init__(bridge)
        self.method = method

    def prepare(self, context, args):
        return {**self.host_service.launch_preview(args["server"]), "method": self.method, "args": args}

    def execute(self, context, args):
        return self.host_service.resource(args["server"], self.method, {k: v for k, v in args.items() if k != "server"})


class McpBridge:
    """连接配置的 MCP server，注册其工具并转发调用。"""

    def __init__(self, workspace, registry, loader, *, transport_factory=None):
        self.workspace = Path(workspace).resolve()
        self.registry = registry
        self.loader = loader
        self.transport_factory = transport_factory or StdioTransport
        self.servers: list[McpServerSpec] = []
        self.transports: dict[str, object] = {}
        self.diagnostics: list[str] = []
        self.config: dict[str, McpServerSpec] = {}
        self.tool_owners: dict[str, tuple[str, str]] = {}

    def configure(self, cache: dict) -> "McpBridge":
        self.cache = cache
        try:
            self.config = {spec.name: spec for spec in load_servers(self.workspace)}
        except McpError as exc:
            self.diagnostics.append(str(exc))
            return self
        if self.config:
            name = "connect_mcp"
            spec = ToolSpec(parameters=object_schema(["server"], server={"type": "string", "enum": list(self.config)}),
                            permission="shell", side_effect=True, routes=("mcp",))
            self.registry.register(ToolDescriptor(name, True, "mcp", "启动指定 MCP 服务并发现工具", self.workspace, spec))
            self.loader.register(name, ConnectMcp(self))
        for spec in self.config.values():
            discovery = cache.get(digest(asdict(spec)), {})
            try:
                self._register_tools(spec, discovery.get("tools", []))
            except McpError as exc:
                self.diagnostics.append(str(exc))
        if any(cache.get(digest(asdict(spec)), {}).get("resources") for spec in self.config.values()):
            self._register_resources()
        return self

    def launch_preview(self, server):
        spec = self.config.get(server)
        if spec is None:
            raise McpError("未知 MCP 服务")
        return {"target": server, "command": [spec.command, *spec.args], "config_digest": digest(asdict(spec))}

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
            handshake = transport.request(
                "initialize",
                {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
            )
            transport.notify("notifications/initialized", {})
            capabilities = handshake.get("capabilities", {})
            resources = "resources" in capabilities
            tools = []
            if "tools" in capabilities:
                params, seen = {}, set()
                while True:
                    listing = transport.request("tools/list", params)
                    if not isinstance(listing, dict) or not isinstance(listing.get("tools"), list):
                        raise McpError("MCP 工具目录无效")
                    tools.extend(listing["tools"])
                    if len(tools) > 5000:
                        raise McpError("MCP 工具目录超过上限")
                    cursor = listing.get("nextCursor")
                    if not cursor:
                        break
                    if not isinstance(cursor, str) or cursor in seen or len(seen) >= 100:
                        raise McpError("MCP 工具目录分页异常")
                    seen.add(cursor)
                    params = {"cursor": cursor}
            self._register_tools(spec, tools)
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
            side_effect=not spec.read_only,
        )
        tool_spec.validate()
        return ToolDescriptor(name, True, "mcp", description.strip()[:300], self.workspace, tool_spec)

    @staticmethod
    def _tool_name(server: str, tool: str) -> str:
        raw = f"mcp_{server}_{tool}".lower()
        name = _TOOL_NAME.sub("_", raw)
        if not name or not name[0].isalpha():
            name = "mcp_" + name
        return name[:63]

    def _register_resources(self):
        servers = sorted(name for name, spec in self.config.items()
                         if self.cache.get(digest(asdict(spec)), {}).get("resources"))
        for name, (method, description) in RESOURCE_METHODS.items():
            required = ["server"]
            props = {"server": {"type": "string", "enum": servers}}
            if method == "resources/read":
                required.append("uri")
                props["uri"] = {"type": "string", "minLength": 1, "maxLength": 8192}
            else:
                props["cursor"] = {"type": "string", "maxLength": 4000}
            spec = ToolSpec(parameters=object_schema(required, **props), permission="network", network=True,
                            routes=("mcp",))
            self.registry.register(ToolDescriptor(name, True, "mcp", description, self.workspace, spec))
            self.loader.register(name, ResourceTool(self, method))

    def resource(self, server, method, params):
        self.ensure_connected(server)
        if not self.cache[digest(asdict(self.config[server]))]["resources"]:
            raise ToolError("unsupported_resource", "此服务未声明资源能力")
        result = self._request(server, method, params)
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
        unsupported = {str(b.get('type')) for b in blocks if b.get('type') not in {'text', 'image'}}
        if unsupported:
            raise ToolError('unsupported_content', '当前仅接入文字和图片，MCP 返回了未支持的内容：' + ', '.join(sorted(unsupported)))
        images = []
        image_blocks = [b for b in blocks if b.get('type') == 'image']
        if image_blocks:
            handler = getattr(self, 'image_handler', None)
            if handler is None:
                raise ToolError('image_unavailable', 'MCP 图片服务未绑定')
            config = self.image_config()
            if len(image_blocks) > config.max_images:
                raise ToolError('image_limit', 'MCP 返回图片数量超过限制')
            for block in image_blocks:
                images.append(handler(block))
            if sum(p['byte_size'] for p in images) > config.max_request_image_bytes:
                raise ToolError('image_limit', 'MCP 图片总大小超过限制')
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
            links = _candidate_links(result["structuredContent"])
            if links:
                payload["candidate_links"] = links
        if images:
            payload.update(images=images, source_refs=[p['image_ref'] for p in images])
        return payload

    def close(self) -> None:
        for transport in self.transports.values():
            transport.close()
        self.transports.clear()
        self.servers.clear()
