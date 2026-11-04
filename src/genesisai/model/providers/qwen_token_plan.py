"""千问 Token Plan 的独立凭证与端点，复用 Qwen 的 OpenAI 协议实现。"""

from genesisai.model.providers.qwen import QwenClient


class QwenTokenPlanClient(QwenClient):
    provider = "qwen_token_plan"
    DEFAULT_BASE_URL = "https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1"
    API_KEY_ENV = "QWEN_TOKEN_PLAN"
    BASE_URL_ENV = "QWEN_TOKEN_PLAN_BASE_URL"
