"""MCP 桥接缝：读取 ``.genesis/mcp.json``，连接 MCP server 并动态注册工具。

MCP 工具以 ``category="mcp"`` 注册进同一 :class:`ToolRegistry`，与内置工具
走同一条 executor/permissions/ledger 管线。服务启动和外部调用分别授权。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from genesisai.core.tools.base import ToolDescriptor, ToolSpec, object_schema
from genesisai.shared.security import ToolError, digest


MCP_CONFIG = Path(".genesis") / "mcp.json"
PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "genesisai", "version": "0.1.0"}
_ALLOWED_TYPES = {"string", "integer", "number", "boolean", "array", "object"}
_TOOL_NAME = re.compile(r"[^a-z0-9_]")


class McpError(ValueError):
    """MCP 配置或协议错误。"""


@dataclass(frozen=True)
class McpServerSpec:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict = field(default_factory=dict)
    read_only: bool = True


def load_servers(workspace) -> list[McpServerSpec]:
    """读取工作区 ``.genesis/mcp.json``；缺失时返回空列表。"""
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
        servers.append(
            McpServerSpec(
                name=name,
                command=command,
                args=tuple(args),
                env=dict(env),
                read_only=bool(cfg.get("read_only", True)),
            )
        )
    return servers


class StdioTransport:
    """基于换行分隔 JSON-RPC 的 MCP stdio 传输。"""

    def __init__(self, spec: McpServerSpec):
        env = {**os.environ, **spec.env}
        try:
            self.process = subprocess.Popen(
                [spec.command, *spec.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                env=env,
            )
        except OSError as exc:
            raise McpError(f"MCP server 无法启动：{exc}") from exc
        self._next_id = 0

    def _write(self, message: dict) -> None:
        if self.process.stdin is None:
            raise McpError("MCP server 已关闭连接")
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        line = self.process.stdout.readline() if self.process.stdout else ""
        if not line:
            raise McpError("MCP server 已关闭连接")
        try:
            response = json.loads(line)
        except ValueError as exc:
            raise McpError("MCP 响应不是合法 JSON") from exc
        if isinstance(response, dict) and response.get("error"):
            raise McpError(f"MCP 调用失败：{response['error']}")
        return response.get("result", {}) if isinstance(response, dict) else {}

    def notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                self.process.terminate()
        except (OSError, ValueError):
            pass


class McpToolImplementation:
    """把 MCP ``tools/call`` 包装成标准工具实现契约。"""

    def __init__(self, bridge: "McpBridge", server: str, tool: str):
        self.bridge = bridge
        self.server = server
        self.tool = tool

    def prepare(self, context, args):
        return {**self.bridge.launch_preview(self.server), "target": f"{self.server}/{self.tool}", "args": args}

    def execute(self, context, args):
        self.bridge.ensure_connected(self.server)
        return self.bridge.call(self.server, self.tool, args)


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
                            permission="shell", side_effect=True)
            self.registry.register(ToolDescriptor(name, True, "mcp", "启动指定 MCP 服务并发现工具", self.workspace, spec))
            self.loader.register(name, self)
        for server, spec in self.config.items():
            for tool in cache.get(digest(asdict(spec)), []):
                self.registry.register(self._descriptor(spec, tool))
        return self

    def launch_preview(self, server):
        spec = self.config.get(server)
        if spec is None:
            raise McpError("未知 MCP 服务")
        return {"target": server, "command": [spec.command, *spec.args], "config_digest": digest(asdict(spec))}

    def prepare(self, context, args):
        return self.launch_preview(args["server"])

    def execute(self, context, args):
        server = args["server"]
        self.ensure_connected(server)
        tools = self.cache[digest(asdict(self.config[server]))]
        return {"server": server, "tools": [self._tool_name(server, tool["name"]) for tool in tools]}

    def ensure_connected(self, server):
        if server not in self.transports:
            self._connect(self.config[server])

    def _connect(self, spec: McpServerSpec) -> None:
        transport = self.transport_factory(spec)
        try:
            transport.request(
                "initialize",
                {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
            )
            transport.notify("notifications/initialized", {})
            listing = transport.request("tools/list", {})
            tools = listing.get("tools", []) if isinstance(listing, dict) else []
            discovered = []
            for tool in tools:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"]:
                    continue
                descriptor = self._descriptor(spec, tool)
                self.registry.register(descriptor)
                discovered.append(tool)
        except BaseException:
            transport.close()
            raise
        self.cache[digest(asdict(spec))] = discovered
        self.servers.append(spec)
        self.transports[spec.name] = transport
        if not discovered:
            self.diagnostics.append(f"{spec.name}: 未发现任何工具")

    def _descriptor(self, spec: McpServerSpec, tool: dict) -> ToolDescriptor:
        name = self._tool_name(spec.name, tool["name"])
        description = tool.get("description")
        if not isinstance(description, str) or not description.strip():
            description = f"MCP 工具 {tool['name']}（{spec.name}）"
        tool_spec = ToolSpec(
            parameters=self._normalize_schema(tool.get("inputSchema")),
            permission="network", network=True,
            path_scope="none",
            side_effect=not spec.read_only,
        )
        self.loader.register(name, McpToolImplementation(self, spec.name, tool["name"]))
        return ToolDescriptor(name, True, "mcp", description.strip()[:300], self.workspace, tool_spec)

    @staticmethod
    def _tool_name(server: str, tool: str) -> str:
        raw = f"mcp_{server}_{tool}".lower()
        name = _TOOL_NAME.sub("_", raw)
        if not name or not name[0].isalpha():
            name = "mcp_" + name
        return name[:63]

    @staticmethod
    def _normalize_schema(schema) -> dict:
        """把 MCP inputSchema 归一为执行管线可校验的 object schema。"""
        properties: dict[str, dict] = {}
        if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
            for key, definition in schema["properties"].items():
                if not isinstance(key, str) or not isinstance(definition, dict):
                    continue
                kind = definition.get("type")
                if isinstance(kind, list):
                    kind = next((item for item in kind if item in _ALLOWED_TYPES), None)
                if kind not in _ALLOWED_TYPES:
                    continue
                normalized = {"type": kind}
                if kind == "array":
                    items = definition.get("items")
                    item_type = items.get("type") if isinstance(items, dict) else None
                    normalized["items"] = {"type": item_type if item_type in _ALLOWED_TYPES else "string"}
                properties[key] = normalized
        required = schema.get("required") if isinstance(schema, dict) else None
        required = [name for name in required if isinstance(name, str) and name in properties] if isinstance(required, list) else []
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    def call(self, server: str, tool: str, args: dict) -> dict:
        transport = self.transports.get(server)
        if transport is None:
            raise ToolError("mcp_unavailable", f"MCP server 未连接：{server}")
        try:
            result = transport.request("tools/call", {"name": tool, "arguments": args})
        except McpError as exc:
            raise ToolError("mcp_error", str(exc)) from exc
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
            "text": "\n".join(texts)[:12000],
            "is_error": bool(result.get("isError")),
        }
        if images:
            payload.update(images=images, source_refs=[p['image_ref'] for p in images])
        return payload

    def close(self) -> None:
        for transport in self.transports.values():
            transport.close()
        self.transports.clear()
        self.servers.clear()
