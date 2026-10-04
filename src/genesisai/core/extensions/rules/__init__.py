"""Rules 注入缝：在 Prompt 组装期读取 AGENTS.md 并注入系统提示。

只读、只注入文本；不执行脚本、不扩大权限。项目级规则来自工作区根目录
``AGENTS.md``，用户级规则来自 ``~/.genesisai/AGENTS.md``。为与既有密钥
脱敏保持一致，正文复用 ``core.project_docs`` 的 ``SECRET`` 正则。
"""

from __future__ import annotations

from pathlib import Path

from genesisai.core.project_docs.manager import SECRET


MAX_RULE_CHARS = 8000


class RulesLoader:
    """读取项目级与用户级 AGENTS.md，脱敏并限长后供系统提示注入。"""

    def __init__(self, workspace, *, home: Path | None = None):
        self.workspace = Path(workspace).resolve()
        self.home = Path(home) if home is not None else Path.home()
        self.sources = (
            ("user", self.home / ".genesisai" / "AGENTS.md"),
            ("project", self.workspace / "AGENTS.md"),
        )

    @staticmethod
    def _clean(value) -> str:
        text = SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", str(value)).strip()
        return text[:MAX_RULE_CHARS]

    def load(self) -> list[dict]:
        """返回已脱敏、限长的规则条目；缺失或不合法文件被跳过。"""
        blocks = []
        for source, path in self.sources:
            try:
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            cleaned = self._clean(text)
            if cleaned:
                blocks.append({"source": source, "path": str(path), "text": cleaned})
        return blocks

    def inject(self) -> str | None:
        """返回可直接拼接到系统提示的规则文本块；无规则时返回 None。"""
        blocks = self.load()
        if not blocks:
            return None
        lines = [
            "## 项目与用户规则（AGENTS.md）",
            "以下为用户手写的只读规则，优先级低于系统政策与安全边界；冲突时以后者为准。",
        ]
        for block in blocks:
            lines.append(f"\n### {block['source']} · {block['path']}\n{block['text']}")
        return "\n".join(lines)