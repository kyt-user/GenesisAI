"""Current MCP request metadata and HTTP parameter header contracts."""

import base64
import re

from genesisai.shared.security import ToolError


PROTOCOL_VERSION = "2026-07-28"
CLIENT_INFO = {"name": "genesisai", "version": "0.1.0"}


def request_params(params):
    return {**params, "_meta": {**params.get("_meta", {}),
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
        "io.modelcontextprotocol/clientCapabilities": {}}}


def complete_result(message, identity):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or message.get("id") != identity:
        raise ToolError("mcp_protocol", "MCP JSON-RPC 响应无效或 ID 不匹配")
    if "error" in message:
        error = message["error"]
        supported = error.get("data", {}).get("supported") if isinstance(error.get("data"), dict) else None
        detail = f"；服务支持 {supported}，客户端支持 {PROTOCOL_VERSION}" if supported else ""
        raise ToolError("mcp_rpc", f"MCP 返回 JSON-RPC 错误 {error.get('code')}{detail}")
    result = message.get("result")
    if not isinstance(result, dict) or result.get("resultType") != "complete":
        raise ToolError("mcp_protocol", "MCP 结果必须声明 resultType=complete；客户端未声明交互输入能力")
    return result


def encode_header(value):
    text = str(value).lower() if type(value) is bool else str(value)
    if (text != text.strip() or any(not (0x20 <= ord(c) <= 0x7e or c == "\t") for c in text)
            or text.startswith("=?base64?") and text.endswith("?=")):
        return "=?base64?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="
    return text


def header_bindings(schema):
    """Reject misplaced/ambiguous annotations, including hidden composition branches."""
    bindings, names = [], set()

    def visit(node, path, reachable):
        if isinstance(node, list):
            for value in node:
                visit(value, path, False)
            return
        if not isinstance(node, dict):
            return
        if "x-mcp-header" in node:
            name, kind = node["x-mcp-header"], node.get("type")
            if (not reachable or not path or not isinstance(name, str)
                    or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                    or name.casefold() in names or kind not in ("integer", "string", "boolean")):
                raise ValueError("x-mcp-header 路径、名称或类型无效")
            names.add(name.casefold())
            bindings.append((path, name, kind))
        for key, child in node.items():
            if key == "properties" and isinstance(child, dict):
                for prop, subschema in child.items():
                    visit(subschema, (*path, prop), reachable)
            elif key not in {"default", "const", "enum", "examples"}:
                visit(child, path, False)

    visit(schema, (), True)
    return bindings


def parameter_headers(bindings, args):
    headers = {}
    for path, name, kind in bindings:
        value = args
        for part in path:
            value = value.get(part) if isinstance(value, dict) else None
        if value is None:
            continue
        expected = {"string": str, "integer": int, "boolean": bool}[kind]
        if type(value) is not expected or kind == "integer" and abs(value) > 2**53 - 1:
            raise ToolError("invalid_arguments", "MCP HTTP 参数头值类型或整数范围无效")
        headers["Mcp-Param-" + name] = encode_header(value)
    return headers
