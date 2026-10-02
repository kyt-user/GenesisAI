"""OpenAI 兼容的对话补全公共实现。"""

from collections.abc import Iterator

from openai import OpenAI

from genesisai.model.base import ModelClient
from genesisai.shared.messages import Message, Response, ToolCall


class OpenAICompatibleClient(ModelClient):
    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        timeout: float,
        max_retries: int = 0,
        generation: dict | None = None,
    ):
        if not api_key:
            raise ValueError("模型 API Key 不能为空")
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )
        self.model = model
        self.generation = dict(generation or {})

    def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> Response:
        options = self._request_options(kwargs)
        if tools == []:
            options['tool_choice'] = 'none'
            options.update(self._no_tool_options())
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[self._to_openai_message(message) for message in messages],
            tools=tools,
            **options,
        )
        message = response.choices[0].message
        tool_calls = [
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=call.function.arguments,
            )
            for call in message.tool_calls or []
        ] or None
        return Response(
            content=message.content,
            reasoning=getattr(message, "reasoning_content", None),
            tool_calls=tool_calls,
            usage=response.usage.model_dump() if response.usage else None,
            finish_reason=getattr(response.choices[0], "finish_reason", None),
        )

    def stream_chat(
        self,
        messages: list[Message] | list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> Iterator[str | Response]:
        options = self._request_options(kwargs)
        if tools == []:
            options['tool_choice'] = 'none'
            options.update(self._no_tool_options())
        options.setdefault("stream_options", {"include_usage": True})
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=[
                message if isinstance(message, dict) else self._to_openai_message(message)
                for message in messages
            ],
            tools=tools,
            stream=True,
            **options,
        )
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_parts: dict[int, dict[str, str]] = {}
        usage = None
        finish_reason = None
        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage.model_dump()
            if not chunk.choices:
                continue
            finish_reason = getattr(chunk.choices[0], "finish_reason", None) or finish_reason
            delta = chunk.choices[0].delta
            # DeepSeek 的 reasoning_content 在 content 之前到达 delta。
            reasoning_delta = getattr(delta, "reasoning_content", None)
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)
                yield ("reasoning", reasoning_delta)
                continue
            if delta.content:
                content_parts.append(delta.content)
                yield delta.content
            for call in delta.tool_calls or []:
                current = tool_call_parts.setdefault(
                    call.index,
                    {"id": "", "name": "", "arguments": ""},
                )
                current["id"] = call.id or current["id"]
                if call.function:
                    current["name"] += call.function.name or ""
                    current["arguments"] += call.function.arguments or ""
        tool_calls = [
            ToolCall(
                id=tool_call_parts[index]["id"],
                name=tool_call_parts[index]["name"],
                arguments=tool_call_parts[index]["arguments"],
            )
            for index in sorted(tool_call_parts)
        ] or None
        yield Response(
            content="".join(content_parts),
            reasoning="".join(reasoning_parts) or None,
            tool_calls=tool_calls,
            usage=usage, finish_reason=finish_reason,
        )

    def _request_options(self, overrides: dict) -> dict:
        return {**self._provider_defaults(), **self.generation, **overrides}

    def _provider_defaults(self) -> dict:
        raise NotImplementedError

    def _no_tool_options(self) -> dict:
        return {}

    @staticmethod
    def _to_openai_message(message: Message) -> dict:
        result = {"role": message.role}
        if message.content is not None:
            result["content"] = message.content
        if message.reasoning is not None:
            result["reasoning_content"] = message.reasoning
        if message.tool_calls is not None:
            result["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments,
                    },
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id is not None:
            result["tool_call_id"] = message.tool_call_id
        return result

