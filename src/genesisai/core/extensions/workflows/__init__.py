"""Workflows 缝：``.genesis/workflows/*.md`` 定义可复用工作流。

与 Rules 同为"文本承载"，但不进系统提示，由 ``/workflow <name>`` 按需展开
为一段提示后送入运行循环。
"""

from __future__ import annotations

import re
from pathlib import Path


WORKFLOW_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
MAX_WORKFLOW_CHARS = 12000


class WorkflowError(ValueError):
    """工作流名称或内容无效。"""


class WorkflowRegistry:
    """扫描工作区内的 Markdown 工作流，提供按名展开。"""

    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / ".genesis" / "workflows"
        self.items, self.diagnostics = self._scan()

    def _scan(self) -> tuple[dict, list]:
        items: dict[str, dict] = {}
        diagnostics: list[str] = []
        if not self.root.is_dir():
            return items, diagnostics
        for path in sorted(self.root.glob("*.md")):
            name = path.stem
            if not WORKFLOW_NAME.fullmatch(name):
                diagnostics.append(f"{path.name}: 名称无效")
                continue
            try:
                text = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError) as exc:
                diagnostics.append(f"{path.name}: {exc}")
                continue
            if not text:
                diagnostics.append(f"{path.name}: 内容为空")
                continue
            items[name] = {"name": name, "path": str(path), "text": text[:MAX_WORKFLOW_CHARS]}
        return items, diagnostics

    def names(self) -> list[str]:
        return sorted(self.items)

    def summary(self) -> list[dict]:
        return [{"name": item["name"], "path": item["path"]} for item in (self.items[name] for name in self.names())]

    def expand(self, name: str) -> str:
        if name not in self.items:
            raise WorkflowError(f"未知工作流：{name}")
        return self.items[name]["text"]