"""交互式模型提供商/模型选择与密钥写入。

对齐 cline 的 onboarding 交互逻辑：选择提供商 → 输入密钥 → 选择模型 →
持久化到用户配置目录。取消任一步骤返回 None，不写入任何文件。
"""

from __future__ import annotations

import os

from genesisai.app import select
from genesisai.model.providers.catalog import PROVIDERS, get_provider
from genesisai.model.settings import ProviderSettings, load_settings, save_settings


def mask(key: str) -> str:
    """掩码展示密钥。"""
    if not key:
        return "（未设置）"
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


def provider_choices() -> list[tuple[str, str]]:
    """提供商选择项：展示名 + 说明。"""
    return [(provider.label, provider.description) for provider in PROVIDERS]


def env_key(provider_id: str, environ=None) -> str:
    """从环境变量读取指定提供商的密钥。"""
    provider = get_provider(provider_id)
    if provider is None:
        return ""
    return (environ or os.environ).get(provider.api_key_env, "").strip()


def has_credentials(settings: ProviderSettings | None, environ=None) -> bool:
    """判断当前是否已有可用密钥（持久化或环境变量）。"""
    if settings is None:
        return False
    if settings.api_key:
        return True
    return bool(env_key(settings.provider, environ))


def effective_key(settings: ProviderSettings | None, environ=None) -> str:
    """返回生效密钥：优先持久化，其次环境变量。"""
    if settings is None:
        return ""
    return settings.api_key or env_key(settings.provider, environ)


def run_onboarding(console, *, directory=None, initial: ProviderSettings | None = None):
    """执行完整选择流程；成功返回并持久化，取消返回 None。"""
    initial = initial if initial is not None else load_settings(directory)

    default_provider = 0
    if initial:
        for position, provider in enumerate(PROVIDERS):
            if provider.id == initial.provider:
                default_provider = position
                break
    console.print("[dim]配置将写入用户目录，之后无需再设置开发用 .env。[/dim]")
    chosen = select.select(console, "选择模型提供商", provider_choices(), index=default_provider)
    if chosen is None:
        return None
    provider = PROVIDERS[chosen]

    existing = initial.api_key if (initial and initial.provider == provider.id) else ""
    existing = existing or env_key(provider.id)
    if existing:
        console.print(f"[dim]已检测到 {provider.label} 密钥：{mask(existing)}，直接回车沿用。[/dim]")
    hint = f"获取密钥：{provider.docs_url}" if provider.docs_url else ""
    key = select.prompt_secret(console, f"{provider.label} API Key", hint=hint, required=not existing)
    if key is None:
        return None
    key = key or existing
    if not key:
        console.print("[red]未提供密钥，已取消。[/red]")
        return None

    model_options = [(model.label, f"{model.id} · {model.description}") for model in provider.models]
    model_options.append(("自定义模型 ID…", "手动输入不在列表中的模型"))
    default_model = 0
    if initial and initial.provider == provider.id:
        for position, model in enumerate(provider.models):
            if model.id == initial.model:
                default_model = position
                break
    picked = select.select(console, f"选择 {provider.label} 模型", model_options, index=default_model)
    if picked is None:
        return None
    if picked < len(provider.models):
        model = provider.models[picked].id
    else:
        prior = initial.model if (initial and initial.provider == provider.id) else provider.default_model
        model = select.prompt_text(console, "模型 ID", default=prior, required=True)
        if model is None:
            return None

    generation = dict(initial.generation) if initial else {}
    settings = ProviderSettings(
        provider=provider.id,
        model=model,
        api_key=key,
        base_url=provider.base_url,
        generation=generation,
    )
    path = save_settings(settings, directory)
    console.print(f"[green]✓ 已保存[/green] [dim]{path}[/dim]")
    return settings


def select_model_only(console, settings: ProviderSettings, *, directory=None):
    """仅切换模型（保留提供商与密钥），用于会话内 ``/model``。"""
    provider = get_provider(settings.provider)
    if provider is None:
        return None
    model_options = [(model.label, f"{model.id} · {model.description}") for model in provider.models]
    model_options.append(("自定义模型 ID…", "手动输入不在列表中的模型"))
    default_model = 0
    for position, model in enumerate(provider.models):
        if model.id == settings.model:
            default_model = position
            break
    picked = select.select(console, f"切换 {provider.label} 模型", model_options, index=default_model)
    if picked is None:
        return None
    if picked < len(provider.models):
        model = provider.models[picked].id
    else:
        model = select.prompt_text(console, "模型 ID", default=settings.model, required=True)
        if model is None:
            return None
    updated = ProviderSettings(
        provider=settings.provider,
        model=model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        generation=dict(settings.generation),
    )
    save_settings(updated, directory)
    return updated