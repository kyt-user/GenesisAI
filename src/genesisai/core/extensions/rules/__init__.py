"""Rules 注入缝：在 Prompt 组装期读取 AGENTS.md 并注入系统提示。

只读、只注入文本；不执行脚本、不扩大权限。项目级规则来自工作区根目录
``AGENTS.md``，用户级规则来自 ``~/.genesisai/AGENTS.md``。为与既有密钥
脱敏保持一致，正文复用 ``core.state.io_journal`` 的 ``SECRET`` 正则。
"""

from __future__ import annotations

from pathlib import Path
from genesisai.shared.paths import application_home


from genesisai.core.state.io_journal import _ASSIGNMENT as SECRET




class RulesLoader:
    """Read complete rules for the affected paths; the model context owns budgeting."""

    def __init__(self, workspace, *, home: Path | None = None, application_root: Path | None = None):
        self.workspace = Path(workspace).resolve()
        self.home = Path(home) if home is not None else Path.home()
        self.sources = (
            ("user", (Path(application_root) if application_root else (self.home / ".genesisai" if home else application_home())) / "AGENTS.md"),
            ("project", self.workspace / "AGENTS.md"),
        )

    @staticmethod
    def _clean(value) -> str:
        text = SECRET.sub(lambda match: f"{match.group(1)}[REDACTED]", str(value)).strip()
        return text

    def load(self, paths=()) -> list[dict]:
        """Return root rules plus ancestor scopes, broadest to most specific."""
        blocks = []
        scoped = set()
        for value in paths:
            target = Path(value)
            target = target if target.is_absolute() else self.workspace / target
            target = target.absolute()
            if not target.is_relative_to(self.workspace):
                continue
            directory = target if target.is_dir() else target.parent
            while directory != self.workspace and directory.is_relative_to(self.workspace):
                scoped.add(directory / "AGENTS.md")
                directory = directory.parent
        sources = [*self.sources, *(("directory", p) for p in sorted(scoped, key=lambda p: (len(p.parts), str(p))))]
        for source, path in sources:
            try:
                if path.is_symlink() or path.resolve() != path:
                    raise ValueError(f"AGENTS.md 不能通过链接读取：{path}")
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ValueError(f"无法读取 AGENTS.md：{path}") from exc
            cleaned = self._clean(text)
            if cleaned:
                blocks.append({"source": source, "path": str(path), "scope": "所有任务" if source == "user" else str(path.parent), "text": cleaned})
        return blocks

    def inject(self, paths=()) -> str | None:
        """返回可直接拼接到系统提示的规则文本块；无规则时返回 None。"""
        return self.render(self.load(paths))

    @staticmethod
    def render(blocks):
        if not blocks:
            return None
        lines = [
            "## 项目与用户规则（AGENTS.md）",
            "以下为用户手写的只读规则，优先级低于系统政策与安全边界；冲突时以后者为准。",
        ]
        for block in blocks:
            lines.append(f"\n### {block['source']} · {block['path']} · 生效目录 {block['scope']}\n{block['text']}")
        return "\n".join(lines)
