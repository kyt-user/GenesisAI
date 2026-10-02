"""Ollama 本地模型提供商实现。"""

from genesisai.model.openai_compatible import OpenAICompatibleClient


class OllamaClient(OpenAICompatibleClient):
    provider = "ollama"
    DEFAULT_BASE_URL = "http://localhost:11434/v1"
    DEFAULT_TIMEOUT = 120
    DEFAULT_THINKING = False
    DEFAULT_REASONING_EFFORT = "none"

    def __init__(
        self,
        model: str,
        generation: dict | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 0,
    ):
        super().__init__(
            model=model,
            base_url=base_url,
            api_key="ollama",
            timeout=timeout,
            max_retries=max_retries,
            generation=generation,
        )

    def _provider_defaults(self) -> dict:
        # Ollama 会为支持的模型默认启用思考模式。智能体协议调用需要在
        # 有限时间内取得最终回答（JSON 或工具调用），因此这里默认关闭推理。
        return {"reasoning_effort": self.DEFAULT_REASONING_EFFORT}

