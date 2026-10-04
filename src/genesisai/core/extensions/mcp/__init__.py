"""MCP 桥接缝（动态工具注册）。"""

from genesisai.core.extensions.mcp.bridge import (
    McpBridge,
    McpError,
    McpServerSpec,
    StdioTransport,
    load_servers,
)


__all__ = ["McpBridge", "McpError", "McpServerSpec", "StdioTransport", "load_servers"]