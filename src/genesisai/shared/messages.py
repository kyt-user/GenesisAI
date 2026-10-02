"""模型对话与工具调用使用的数据对象。"""

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(slots=True)
class Message:
    role: str
    content: str | None = None
    reasoning: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class Response:
    content: str | None = None
    reasoning: str | None = None
    tool_calls: list[ToolCall] | None = None
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
