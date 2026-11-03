"""模型密钥的统一优先级：环境中的非空值优先，终端配置作为回退。"""

import os

from genesisai.model.providers.catalog import get_provider


def environment_key(provider_id: str, environ=None) -> str:
    provider = get_provider(provider_id)
    if provider is None:
        return ''
    environment = os.environ if environ is None else environ
    return (environment.get(provider.api_key_env) or '').strip()


def resolve_api_key(provider_id: str, fallback: str | None = None, *, environ=None) -> str:
    return environment_key(provider_id, environ) or (fallback or '').strip()
