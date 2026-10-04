"""阿里云通义千问（DashScope OpenAI 兼容）模型提供商实现。"""

import os

from genesisai.model.openai_compatible import OpenAICompatibleClient


class QwenClient(OpenAICompatibleClient):
    provider = "qwen"
    DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DEFAULT_TIMEOUT = 60
    DEFAULT_THINKING = None
    DEFAULT_REASONING_EFFORT = None
    API_KEY_ENV = "DASHSCOPE_API_KEY"
    BASE_URL_ENV = "QWEN_BASE_URL"

    def __init__(
        self,
        model: str,
        generation: dict | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 0,
    ):
        resolved_key = (api_key or os.environ.get(self.API_KEY_ENV, "")).strip()
        if not resolved_key:
            raise RuntimeError(f"缺少环境变量 {self.API_KEY_ENV}")
        resolved_url = (
            base_url
            or os.environ.get(self.BASE_URL_ENV, "").strip()
            or self.DEFAULT_BASE_URL
        )
        super().__init__(
            model=model,
            base_url=resolved_url,
            api_key=resolved_key,
            timeout=timeout,
            max_retries=max_retries,
            generation=generation,
        )

    def _provider_defaults(self) -> dict:
        return {}