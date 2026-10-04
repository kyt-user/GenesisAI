"""云模型提供商目录：供 CLI 交互式选择与配置写入使用。

目录只描述可选提供商及其模型，不持有任何密钥；密钥由 CLI 在运行时采集，
经 ``genesisai.model.settings`` 持久化。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelOption:
    """单个可选模型。"""

    id: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class ProviderOption:
    """单个云模型提供商。"""

    id: str
    label: str
    description: str
    api_key_env: str
    base_url: str
    models: tuple[ModelOption, ...]
    default_model: str
    key_hint: str = ""
    docs_url: str = ""


PROVIDERS: tuple[ProviderOption, ...] = (
    ProviderOption(
        id="deepseek",
        label="DeepSeek",
        description="DeepSeek 官方 API（OpenAI 兼容）",
        api_key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
        models=(
            ModelOption("deepseek-v4-flash", "DeepSeek V4 Flash", "快速、低成本的默认模型"),
            ModelOption("deepseek-chat", "DeepSeek Chat", "通用对话模型"),
            ModelOption("deepseek-reasoner", "DeepSeek Reasoner", "深度推理模型"),
        ),
        default_model="deepseek-v4-flash",
        key_hint="sk-...",
        docs_url="https://platform.deepseek.com/api_keys",
    ),
    ProviderOption(
        id="qwen",
        label="通义千问 Qwen",
        description="阿里云 DashScope（OpenAI 兼容）",
        api_key_env="DASHSCOPE_API_KEY",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        models=(
            ModelOption("qwen-plus", "Qwen Plus", "均衡通用模型"),
            ModelOption("qwen-max", "Qwen Max", "能力最强"),
            ModelOption("qwen-turbo", "Qwen Turbo", "速度优先"),
        ),
        default_model="qwen-plus",
        key_hint="sk-...",
        docs_url="https://bailian.console.aliyun.com/",
    ),
)

_BY_ID = {provider.id: provider for provider in PROVIDERS}


def get_provider(provider_id: str) -> ProviderOption | None:
    """按 id 返回提供商，未知返回 None。"""
    return _BY_ID.get((provider_id or "").strip())


def provider_ids() -> list[str]:
    """返回全部提供商 id，保持目录顺序。"""
    return [provider.id for provider in PROVIDERS]