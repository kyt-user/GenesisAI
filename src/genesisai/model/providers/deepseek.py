"""DeepSeek 模型提供商实现。"""

import os

from genesisai.model.openai_compatible import OpenAICompatibleClient


class DeepSeekClient(OpenAICompatibleClient):
    provider = "deepseek"
    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_TIMEOUT = 60
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
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 0,
        reasoning_enabled: bool = True,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    ):
        resolved_key = (api_key or os.environ.get(self.API_KEY_ENV, "")).strip()
        if not resolved_key:
            raise RuntimeError(f"缺少环境变量 {self.API_KEY_ENV}")
        super().__init__(
            model=model,
            base_url=base_url,
            api_key=resolved_key,
            timeout=timeout,
            max_retries=max_retries,
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
        # 最终回答和恢复轮次不能调用工具。在此禁用提供商侧思考，
        # 同时避免文本 DSML 尝试并降低成本。
        return {"extra_body": {"thinking": {"type": "disabled"}}}

