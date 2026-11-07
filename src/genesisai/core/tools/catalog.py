"""工具目录：管理启用状态、依赖与运行资格。"""

from __future__ import annotations

from genesisai.shared.security import ToolError
from genesisai.core.tools.registry import ToolRegistry


class ToolCatalog:
    """工具目录：根据 enabled、平台和依赖提供可用工具清单。"""

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

    def available_tools(self):
        return [item for item in self.registry.values() if self._disabled_reason(item) is None]

    def inventory(self) -> dict[str, list[dict]]:
        """来源是一级分类；可用性是每项独立的运行状态。"""
        groups: dict[str, list[dict]] = {"builtin": [], "external": []}
        for descriptor in self.registry.values():
            reason = self._disabled_reason(descriptor)
            entry = {
                "name": descriptor.name,
                "category": descriptor.category,
                "description": descriptor.description,
                "source": descriptor.source,
                "scope": descriptor.scope,
                "provider": descriptor.provider,
                "enabled": reason is None,
            }
            if reason:
                entry["reason"] = reason
            groups[descriptor.source].append(entry)
        return groups
