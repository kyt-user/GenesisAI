"""CLI 管理的用户级模型配置持久化。

用户在终端完成「提供商 → 密钥 → 模型」选择后，配置写入用户配置目录下的
``config.json``，使其不再依赖开发期的 ``.env``。进程环境变量仍作为回退，
优先级：持久化配置 > 环境变量。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


CONFIG_DIR_ENV = "GENESISAI_CONFIG_DIR"
SETTINGS_FILENAME = "config.json"
SETTINGS_SCHEMA_VERSION = 1


def default_config_dir() -> Path:
    """用户配置目录：默认 ``~/.genesisai``，可用环境变量覆盖。"""
    override = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".genesisai"


def settings_path(directory=None) -> Path:
    """配置文件完整路径。"""
    base = Path(directory).expanduser() if directory else default_config_dir()
    return base / SETTINGS_FILENAME


@dataclass
class ProviderSettings:
    """一次 CLI 选择的模型配置。"""

    provider: str
    model: str
    api_key: str = ""
    base_url: str = ""
    generation: dict = field(default_factory=dict)

    def masked_key(self) -> str:
        """返回可展示的掩码密钥。"""
        if len(self.api_key) <= 8:
            return "****" if self.api_key else "（未设置）"
        return f"{self.api_key[:4]}…{self.api_key[-4:]}"


def load_settings(directory=None) -> ProviderSettings | None:
    """读取持久化配置；文件缺失或损坏返回 None。"""
    path = settings_path(directory)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    provider = str(data.get("provider") or "").strip()
    model = str(data.get("model") or "").strip()
    if not provider or not model:
        return None
    generation = data.get("generation")
    return ProviderSettings(
        provider=provider,
        model=model,
        api_key=str(data.get("api_key") or "").strip(),
        base_url=str(data.get("base_url") or "").strip(),
        generation=dict(generation) if isinstance(generation, dict) else {},
    )


def save_settings(settings: ProviderSettings, directory=None) -> Path:
    """原子写入配置，并在 POSIX 上收紧权限。"""
    path = settings_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SETTINGS_SCHEMA_VERSION,
        "provider": settings.provider,
        "model": settings.model,
        "api_key": settings.api_key,
        "base_url": settings.base_url,
        "generation": dict(settings.generation),
    }
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    try:  # 尽量限制为仅当前用户可读
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path