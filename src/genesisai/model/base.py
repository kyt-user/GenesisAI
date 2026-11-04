"""智能体共用的大语言模型客户端抽象。"""

from abc import ABC, abstractmethod
from collections.abc import Iterator

from genesisai.shared.messages import Message, Response


class ModelClient(ABC):
    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> Response:
        """调用模型并返回统一响应。"""

    @abstractmethod
    def stream_chat(
        self,
        messages: list[Message] | list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> Iterator[str | tuple[str, str] | Response]:
        """依次产生正文片段、可选推理片段和一个完整的最终响应。"""
