"""三层、只读、按需加载的 Skill 注册表。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import yaml


SKILL_FIELDS = frozenset({"name", "description", "entry", "tools"})
SKILL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class SkillError(ValueError):
    """Skill 清单或入口无效。"""


@dataclass(frozen=True)
class SkillDescriptor:
    name: str
    description: str
    entry: Path
    tools: tuple[str, ...]
    source: str
    digest: str
    overridden: tuple[str, ...] = ()


class SkillRegistry:
    def __init__(self, workspace: Path, tool_registry=None, *, builtin: Path | None = None, user: Path | None = None):
        self.workspace = Path(workspace).resolve()
        self.tool_registry = tool_registry
        self.roots = [
            ("builtin", Path(builtin or files("genesisai").joinpath("skills/builtin"))),
            ("user", Path(user or Path.home() / ".genesisai" / "skills")),
            ("project", self.workspace / ".genesis" / "skills"),
        ]
        self.items, self.diagnostics = self._scan()

    def _scan(self):
        selected = {}
        shadowed = {}
        diagnostics = []
        for source, root in self.roots:
            try:
                manifests = sorted(root.glob("*/skill.yaml")) if root.is_dir() else []
            except OSError as exc:
                diagnostics.append(f"{source}: {exc}")
                continue
            for manifest in manifests:
                try:
                    descriptor = self._load(manifest, source)
                    if descriptor.name in selected:
                        shadowed.setdefault(descriptor.name, []).append(selected[descriptor.name].source)
                    selected[descriptor.name] = descriptor
                except (OSError, UnicodeError, yaml.YAMLError, SkillError) as exc:
                    diagnostics.append(f"{manifest}: {exc}")
        result = {}
        for name, item in selected.items():
            result[name] = SkillDescriptor(**{**item.__dict__, "overridden": tuple(shadowed.get(name, []))})
        return result, diagnostics

    def _load(self, manifest, source):
        root = manifest.parent.resolve()
        if root.is_symlink():
            raise SkillError("Skill 目录不能是链接")
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != SKILL_FIELDS:
            raise SkillError("skill.yaml 必须且只能包含 name、description、entry、tools")
        name, description, entry, tools = data["name"], data["description"], data["entry"], data["tools"]
        if not isinstance(name, str) or not SKILL_NAME.fullmatch(name):
            raise SkillError("Skill name 无效")
        if not isinstance(description, str) or not description.strip() or len(description) > 500:
            raise SkillError("Skill description 无效")
        if not isinstance(entry, str) or not entry or Path(entry).is_absolute():
            raise SkillError("Skill entry 无效")
        entry_path = (root / entry).resolve(strict=True)
        if not entry_path.is_relative_to(root) or entry_path.suffix.lower() != ".md":
            raise SkillError("Skill entry 越界或不是 Markdown")
        if not isinstance(tools, list) or len(tools) > 20 or any(not isinstance(item, str) for item in tools):
            raise SkillError("Skill tools 无效")
        if self.tool_registry is not None:
            missing = [item for item in tools if item not in self.tool_registry or not self.tool_registry.get(item).enabled]
            if missing:
                raise SkillError("Skill 引用了不可用工具：" + ", ".join(missing))
        raw = manifest.read_bytes() + entry_path.read_bytes()
        return SkillDescriptor(name, description.strip(), entry_path, tuple(tools), source, hashlib.sha256(raw).hexdigest())

    def search(self, query="", limit=20):
        terms = query.casefold().split()
        values = []
        for item in self.items.values():
            text = f"{item.name} {item.description}".casefold()
            if terms and not any(term in text for term in terms):
                continue
            values.append(self.describe(item.name, include_content=False))
        return sorted(values, key=lambda value: value["name"])[:limit]

    def describe(self, name, *, include_content=False):
        try: item = self.items[name]
        except KeyError as exc: raise SkillError("未知 Skill") from exc
        value = {"name": item.name, "description": item.description, "tools": list(item.tools), "source": item.source, "hash": item.digest, "overridden": list(item.overridden)}
        if include_content:
            value["content"] = item.entry.read_text(encoding="utf-8")[:12000]
        return value

    def load(self, names, current=None):
        if not isinstance(names, list) or not 1 <= len(names) <= 2:
            raise SkillError("每次最多加载一个主 Skill 和一个辅助 Skill")
        loaded = list(current or [])
        for name in names:
            if name not in self.items:
                raise SkillError(f"未知 Skill：{name}")
            if name not in loaded:
                loaded.append(name)
        if len(loaded) > 2:
            raise SkillError("已加载 Skill 达到上限 2")
        return loaded
