"""Builtin instructions and explicitly registered external Skills."""

from dataclasses import dataclass
import hashlib
from importlib.resources import files
from pathlib import Path
import re

import yaml

from genesisai.shared.skill_graph import validate_stages, resolve_stage, stage_order


SKILL_FIELDS = frozenset({"name", "description", "entry", "stages"})
SKILL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class SkillDescriptor:
    name: str
    description: str
    entry: Path
    stages: dict
    source: str
    scope: str
    digest: str
    capability_id: str | None = None
    revision: int | None = None


class SkillRegistry:
    def __init__(self, tool_registry=None, *, builtin=None):
        self.tool_registry = tool_registry
        self.root = Path(builtin or files("genesisai").joinpath("core/extensions/skills/builtin"))
        self.items, self.diagnostics = {}, []
        for manifest in sorted(self.root.glob("*/skill.yaml")):
            try:
                self._builtin(manifest)
            except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
                self.diagnostics.append(f"{manifest}: {exc}")

    def _builtin(self, manifest):
        root = manifest.parent
        if root.is_symlink() or bool(getattr(root.lstat(), "st_file_attributes", 0) & 0x400):
            raise SkillError("Skill 目录不能是链接")
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != SKILL_FIELDS:
            raise SkillError("skill.yaml 必须且只能包含 name、description、entry、stages")
        name, description, entry, stages = (data[k] for k in ("name", "description", "entry", "stages"))
        if not isinstance(name, str) or not SKILL_NAME.fullmatch(name) or name in self.items:
            raise SkillError("Skill name 无效或重复")
        if not isinstance(description, str) or not description.strip() or len(description) > 500:
            raise SkillError("Skill description 无效")
        if not isinstance(entry, str) or not entry or Path(entry).is_absolute():
            raise SkillError("Skill entry 无效")
        path = (root / entry).resolve(strict=True)
        if not path.is_relative_to(root.resolve()) or path.suffix.lower() != ".md":
            raise SkillError("Skill entry 越界或不是 Markdown")
        self._validate_stages(stages)
        digest = hashlib.sha256(manifest.read_bytes() + path.read_bytes()).hexdigest()
        self.items[name] = SkillDescriptor(name, description.strip(), path, stages, "builtin", "application", digest)

    @staticmethod
    def _validate_stages(stages):
        try:
            validate_stages(stages)
        except ValueError as exc:
            raise SkillError(str(exc)) from exc

    def _available(self, name):
        return self.tool_registry is None or (name in self.tool_registry and self.tool_registry.get(name).enabled)

    def resolve(self, name, stage, *, available=None):
        if name not in self.items:
            raise SkillError('未知或未注册的 Skill：' + name)
        try:
            return resolve_stage(self.items[name].stages, stage, available or self._available)
        except ValueError as exc:
            raise SkillError(str(exc)) from exc

    def active_tools(self, active, *, available=None):
        tools = set()
        for name, stage in active.items():
            if name in self.items:
                try:
                    tools.update(self.resolve(name, stage, available=available)['tools'])
                except SkillError as exc:
                    message = f'{name}/{stage}: {exc}'
                    if message not in self.diagnostics:
                        self.diagnostics.append(message)
        return tools

    def replace_external(self, entries):
        selected = {name: item for name, item in self.items.items() if item.source == "builtin"}
        for item, bundle in entries:
            package = item["package"]
            name = package["name"]
            if name in selected:
                raise SkillError("Skill 名称冲突，禁止隐式覆盖：" + name)
            self._validate_stages(package["definition"]["stages"])
            selected[name] = SkillDescriptor(name, package["description"], Path(bundle) / package["definition"]["entry"],
                                             package["definition"]["stages"], "external", package["scope"], item["digest"], item["id"], item["revision"])
        self.items = selected

    def search(self, query="", limit=20):
        terms = query.casefold().split()
        result = [self.describe(item.name) for item in self.items.values()
                  if not terms or any(term in f"{item.name} {item.description}".casefold() for term in terms)]
        return sorted(result, key=lambda item: (item["source"], item["name"]))[:limit]

    def describe(self, name, *, include_content=False):
        if name not in self.items:
            raise SkillError("未知或未注册的 Skill")
        item = self.items[name]
        result = {"name": item.name, "description": item.description, "stages": item.stages, "source": item.source,
                  "scope": item.scope, "hash": item.digest, "capability_id": item.capability_id, "revision": item.revision}
        if include_content:
            result["content"] = item.entry.read_text(encoding="utf-8")
        return result

    def load(self, name, stage, current=None, *, available=None):
        self.resolve(name, stage, available=available)
        return {**(current or {}), name: stage}
