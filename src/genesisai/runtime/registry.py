"""扫描受信任内置工具目录，并建立严格的工具注册表。"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import yaml

from genesisai.runtime.base import ToolDescriptor, ToolSpec


TOOL_FIELDS = frozenset({"name", "enabled", "category", "description"})
TOOL_CATEGORIES = frozenset({"core", "filesystem", "web", "development", "git", "memory", "office", "novel"})
TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
CORE_NAMES = ("tool_search", "tool_describe", "tool_load", "skill_search", "skill_describe", "skill_load")
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
CATEGORY_PATHS = {
    "core": PACKAGE_ROOT / "runtime" / "builtin_tools" / "core",
    "filesystem": PACKAGE_ROOT / "capabilities" / "filesystem",
    "development": PACKAGE_ROOT / "capabilities" / "shell",
    "git": PACKAGE_ROOT / "capabilities" / "git",
    "web": PACKAGE_ROOT / "capabilities" / "web",
    "memory": PACKAGE_ROOT / "capabilities" / "memory_tools",
    "office": PACKAGE_ROOT / "capabilities" / "office",
    "novel": PACKAGE_ROOT / "capabilities" / "novel",
}
CATEGORY_MODULES = {
    "core": "genesisai.runtime.builtin_tools.core",
    "filesystem": "genesisai.capabilities.filesystem",
    "development": "genesisai.capabilities.shell",
    "git": "genesisai.capabilities.git",
    "web": "genesisai.capabilities.web",
    "memory": "genesisai.capabilities.memory_tools",
    "office": "genesisai.capabilities.office",
    "novel": "genesisai.capabilities.novel",
}


class RegistryError(ValueError):
    """内置工具目录或 Spec 无效。"""


class ToolRegistry:
    """扫描受信任内置工具目录，维护严格的工具描述符注册表。"""
    def __init__(self, root: Path | None = None):
        self.root = Path(root).resolve() if root else PACKAGE_ROOT
        self.category_roots = (
            {category: path.resolve() for category, path in CATEGORY_PATHS.items()}
            if root is None
            else None
        )
        self._items = self._scan()

    def refresh(self) -> None:
        """重新读取内置 YAML，使启用状态变更进入后续调用。"""
        self._items = self._scan()

    def _scan(self) -> dict[str, ToolDescriptor]:
        items: dict[str, ToolDescriptor] = {}
        manifests: list[tuple[str | None, Path, Path]] = []
        if self.category_roots is None:
            manifests = [(None, self.root, manifest) for manifest in sorted(self.root.glob("*/*/tool.yaml"))]
        else:
            for category, root in sorted(self.category_roots.items()):
                manifests.extend((category, root, manifest) for manifest in sorted(root.glob("*/tool.yaml")))
        for expected_category, root, manifest in manifests:
            directory = manifest.parent
            if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
                raise RegistryError(f"工具目录不能是链接：{directory}")
            resolved = manifest.resolve()
            if not resolved.is_relative_to(root):
                raise RegistryError(f"工具配置越出内置目录：{manifest}")
            try:
                data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError) as exc:
                raise RegistryError(f"工具 YAML 无法读取：{manifest}") from exc
            descriptor = self._descriptor(manifest, data, expected_category=expected_category)
            if descriptor.name in items:
                raise RegistryError(f"工具名称重复：{descriptor.name}")
            items[descriptor.name] = descriptor

        if not items:
            raise RegistryError("未发现内置工具")
        if set(CORE_NAMES) - set(items):
            raise RegistryError("缺少 Core 工具")
        return items

    def _descriptor(self, manifest: Path, data: object, *, expected_category: str | None = None) -> ToolDescriptor:
        if not isinstance(data, dict):
            raise RegistryError(f"工具 YAML 根节点必须是对象：{manifest}")
        if set(data) != TOOL_FIELDS:
            missing = sorted(TOOL_FIELDS - set(data))
            unknown = sorted(set(data) - TOOL_FIELDS)
            detail = "; ".join(filter(None, [
                "缺少 " + ", ".join(missing) if missing else "",
                "未知 " + ", ".join(unknown) if unknown else "",
            ]))
            raise RegistryError(f"工具 YAML 字段无效：{manifest}（{detail}）")

        name = data["name"]
        category = data["category"]
        description = data["description"]
        enabled = data["enabled"]
        if not isinstance(name, str) or not TOOL_NAME.fullmatch(name):
            raise RegistryError(f"工具 name 无效：{manifest}")
        if name != manifest.parent.name:
            raise RegistryError(f"工具 name 与目录不一致：{manifest}")
        directory_category = expected_category or manifest.parent.parent.name
        if not isinstance(category, str) or category not in TOOL_CATEGORIES or category != directory_category:
            raise RegistryError(f"工具 category 无效或与目录不一致：{manifest}")
        if type(enabled) is not bool:
            raise RegistryError(f"工具 enabled 必须是布尔值：{manifest}")
        if not isinstance(description, str) or not description.strip() or len(description) > 500:
            raise RegistryError(f"工具 description 无效：{manifest}")
        if category == "core" and (name not in CORE_NAMES or not enabled):
            raise RegistryError(f"Core 工具名称或启用状态无效：{manifest}")
        if category != "core" and name in CORE_NAMES:
            raise RegistryError(f"Core 工具分类无效：{manifest}")

        spec_path = manifest.parent / "spec.py"
        implementation_path = manifest.parent / "implementation.py"
        if not spec_path.is_file() or not implementation_path.is_file():
            raise RegistryError(f"工具缺少 spec.py 或 implementation.py：{manifest.parent}")
        module_name = f"{CATEGORY_MODULES[category]}.{name}.spec"
        try:
            module = importlib.import_module(module_name)
            spec = module.SPEC
        except Exception as exc:
            raise RegistryError(f"工具 Spec 无法加载：{name}") from exc
        if not isinstance(spec, ToolSpec):
            raise RegistryError(f"工具 SPEC 类型无效：{name}")
        try:
            spec.validate()
        except Exception as exc:
            raise RegistryError(f"工具 Spec 无效：{name}") from exc
        if category == "core" and spec.permission != "core":
            raise RegistryError(f"Core 工具 Spec 权限无效：{name}")
        if category != "core" and spec.permission == "core":
            raise RegistryError(f"业务工具不能使用 Core 权限：{name}")
        if category == "web" and spec.permission != "network":
            raise RegistryError(f"Web 工具必须使用网络权限：{name}")
        if category == "filesystem" and spec.permission not in {"read", "write"}:
            raise RegistryError(f"文件工具权限无效：{name}")
        return ToolDescriptor(name, enabled, category, description.strip(), manifest.parent, spec)

    def get(self, name: str) -> ToolDescriptor:
        try:
            return self._items[name]
        except KeyError as exc:
            raise RegistryError(f"未知工具：{name}") from exc

    def values(self) -> tuple[ToolDescriptor, ...]:
        return tuple(self._items[name] for name in sorted(self._items))

    def __contains__(self, name: str) -> bool:
        return name in self._items

    def __len__(self) -> int:
        return len(self._items)

