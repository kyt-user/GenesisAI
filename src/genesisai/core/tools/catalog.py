"""工具目录：判定启用/可用并全量下发工具定义。"""

from __future__ import annotations

from genesisai.shared.security import ToolError
from genesisai.core.tools.registry import ToolRegistry


class ToolCatalog:
    """工具目录：以 enabled 与平台/依赖可用性为唯一判据，全量下发定义。"""

    def __init__(self, registry: ToolRegistry, loader, store):
        self.registry = registry
        self.loader = loader
        self.store = store

    def refresh(self) -> None:
        self.registry.refresh()

    @staticmethod
    def _disabled_reason(descriptor) -> str | None:
        if not descriptor.enabled:
            return "已在 tool.yaml 中禁用"
        return descriptor.spec.unavailable_reason()

    def available(self, name: str) -> bool:
        """工具存在且当前启用、平台与依赖均满足。"""
        if name not in self.registry:
            return False
        return self._disabled_reason(self.registry.get(name)) is None

    def descriptor_for_execution(self, name: str):
        if name not in self.registry:
            raise ToolError("unknown_tool", "未知工具")
        descriptor = self.registry.get(name)
        reason = self._disabled_reason(descriptor)
        if reason:
            raise ToolError("tool_unavailable", f"工具不可用：{reason}")
        return descriptor

    def definitions(self) -> list[dict]:
        """全量下发：返回所有启用且平台/依赖可用的工具完整定义。"""
        return [
            descriptor.definition()
            for descriptor in self.registry.values()
            if self._disabled_reason(descriptor) is None
        ]

    def inventory(self) -> dict[str, list[dict]]:
        """CLI 用清单：将工具分为启用与禁用两组。"""
        groups: dict[str, list[dict]] = {"enabled": [], "disabled": []}
        for descriptor in self.registry.values():
            reason = self._disabled_reason(descriptor)
            entry = {
                "name": descriptor.name,
                "category": descriptor.category,
                "description": descriptor.description,
            }
            if reason:
                entry["reason"] = reason
                groups["disabled"].append(entry)
            else:
                groups["enabled"].append(entry)
        return groups