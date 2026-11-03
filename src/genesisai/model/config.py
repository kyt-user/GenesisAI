"""从 YAML 加载供用户修改的精简模型配置。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROVIDERS = frozenset({"deepseek", "qwen"})
TOP_LEVEL_KEYS = frozenset({"provider", "model", "generation", "context"})
GENERATION_KEYS = frozenset({"max_tokens", "temperature", "top_p"})


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    generation: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)


def load_model_config(path=None) -> ModelConfig:
    source = Path(path) if path else PROJECT_ROOT / "config/model.yaml"
    if not source.is_file():
        raise ValueError(f"模型配置不存在：{source}")
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("模型配置必须是 YAML 对象")
    unknown = sorted(set(data) - TOP_LEVEL_KEYS)
    if unknown:
        raise ValueError(f"模型配置包含未知字段: {unknown}")

    provider = data.get("provider")
    model = data.get("model")
    generation = data.get("generation", {})
    if provider not in PROVIDERS:
        raise ValueError(f"不支持的 Provider：{provider}")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model 必须是非空字符串")
    if not isinstance(generation, dict):
        raise ValueError("generation 必须是 YAML 对象")
    unknown_generation = sorted(set(generation) - GENERATION_KEYS)
    if unknown_generation:
        raise ValueError(f"generation 包含未知字段: {unknown_generation}")
    for key, value in generation.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"generation.{key} 必须是数值")
    if "max_tokens" in generation and not 1 <= generation["max_tokens"] <= 384000:
        raise ValueError("generation.max_tokens 超出范围")
    for key in ("temperature", "top_p"):
        if key in generation and not 0 <= generation[key] <= 2:
            raise ValueError(f"generation.{key} 超出范围")
    from genesisai.shared.context_config import ContextConfig
    context = data.get('context', {})
    ContextConfig.parse(context)
    return ModelConfig(provider=provider, model=model.strip(), generation=dict(generation), context=context)


def build_model(path=None, settings=None):
    """构建模型客户端。

    传入 ``settings`` 时采用其 provider/model；密钥始终优先环境变量，
    settings.api_key 仅作回退。无 settings 时从 YAML 获取模型配置。
    """
    if settings is not None:
        return _build_from_settings(settings)
    config = load_model_config(path)
    from genesisai.model.providers.deepseek import DeepSeekClient
    from genesisai.model.providers.qwen import QwenClient

    classes = dict(deepseek=DeepSeekClient, qwen=QwenClient)
    client = classes[config.provider](model=config.model, generation=config.generation)
    client.context_config = config.context
    host = urlsplit(str(client.client.base_url)).hostname
    remote = host not in {"localhost", "127.0.0.1", "::1"}
    return client, remote


def _build_from_settings(settings):
    """按 CLI 持久化配置选择模型；提供商统一按环境优先解析密钥。"""
    from genesisai.model.providers.deepseek import DeepSeekClient
    from genesisai.model.providers.qwen import QwenClient

    classes = dict(deepseek=DeepSeekClient, qwen=QwenClient)
    provider = (settings.provider or "").strip()
    if provider not in classes:
        raise ValueError(f"不支持的 Provider：{provider}")
    if not (settings.model or "").strip():
        raise ValueError("model 必须是非空字符串")
    kwargs = {
        "model": settings.model.strip(),
        "generation": dict(settings.generation or {}),
        "api_key": settings.api_key or None,
    }
    if settings.base_url:
        kwargs["base_url"] = settings.base_url
    client = classes[provider](**kwargs)
    from genesisai.shared.context_config import ContextConfig
    ContextConfig.parse(settings.context)
    client.context_config = dict(settings.context)
    host = urlsplit(str(client.client.base_url)).hostname
    remote = host not in {"localhost", "127.0.0.1", "::1"}
    return client, remote

