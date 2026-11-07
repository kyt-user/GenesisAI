"""DeepSeek 模型提供商实现。"""

from genesisai.model.credentials import resolve_api_key
from genesisai.model.openai_compatible import OpenAICompatibleClient


class DeepSeekClient(OpenAICompatibleClient):
    provider = "deepseek"
    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_THINKING = {"type": "enabled"}
    DEFAULT_REASONING_EFFORT = "low"
    API_KEY_ENV = "DEEPSEEK_API_KEY"

    def __init__(
        self,
        model: str,
        generation: dict | None = None,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        request_timeouts: dict | None = None,
        reasoning_enabled: bool = True,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    ):
        resolved_key = resolve_api_key(self.provider, api_key)
        if not resolved_key:
            raise RuntimeError(f"缺少环境变量 {self.API_KEY_ENV}")
        super().__init__(
            model=model,
            base_url=base_url,
            api_key=resolved_key,
            request_timeouts=request_timeouts,
            generation=generation,
        )
        self.reasoning_enabled = reasoning_enabled
        self.reasoning_effort = reasoning_effort

    def _provider_defaults(self) -> dict:
        return {
            "extra_body": {"thinking": {"type": "enabled" if self.reasoning_enabled else "disabled"}},
            "reasoning_effort": self.reasoning_effort,
        }

    def _no_tool_options(self) -> dict:
        # An explicitly tool-free request can disable provider-side thinking.
        return {"extra_body": {"thinking": {"type": "disabled"}}}

    def _request_messages(self, messages):
        values = super()._request_messages(messages)
        for item in values:
            if item.get("role") == "assistant":
                # Host-generated tool turns have no model reasoning. The current
                # thinking-mode wire contract still requires the field to exist.
                item.setdefault("reasoning_content", "")
        return values

