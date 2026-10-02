"""按内置目录约定延迟加载工具实现。"""

from __future__ import annotations

import importlib
from genesisai.shared.security import ToolError
from genesisai.runtime.base import ToolDescriptor
from genesisai.runtime.registry import CATEGORY_MODULES


class ToolLoader:
    """按内置目录约定延迟加载工具实现模块。"""
    def __init__(self):
        self._cache: dict[str, object] = {}

    def load(self, descriptor: ToolDescriptor):
        if descriptor.name in self._cache:
            return self._cache[descriptor.name]
        module_name = f"{CATEGORY_MODULES[descriptor.category]}.{descriptor.name}.implementation"
        try:
            module = importlib.import_module(module_name)
            implementation = module.Implementation()
        except Exception as exc:
            raise ToolError("tool_load_failed", f"工具实现加载失败：{descriptor.name}") from exc
        if not callable(getattr(implementation, "prepare", None)) or not callable(
            getattr(implementation, "execute", None)
        ):
            raise ToolError("tool_load_failed", f"工具实现契约无效：{descriptor.name}")
        self._cache[descriptor.name] = implementation
        return implementation

    def load_many(self, descriptors: list[ToolDescriptor]) -> None:
        before = set(self._cache)
        try:
            for descriptor in descriptors:
                self.load(descriptor)
        except Exception:
            for name in set(self._cache) - before:
                self._cache.pop(name, None)
            raise

    def loaded(self, name: str) -> bool:
        return name in self._cache

    def loaded_names(self) -> set[str]:
        return set(self._cache)

    def discard_newer_than(self, checkpoint: set[str]) -> None:
        for name in set(self._cache) - checkpoint:
            self._cache.pop(name, None)

