"""为章节文件生成结构化大纲摘要。"""

import re
from pathlib import Path

from genesisai.shared.security import ToolError

_HEADING = re.compile(r"^(#+)\s+(.+)", re.MULTILINE)
_PARAGRAPH = re.compile(r"^(?!\s*$|#+)(.+?)(?=\n\n|\n#|\Z)", re.MULTILINE | re.DOTALL)


class Implementation:
    def prepare(self, context, args):
        return {"name": "chapter_outline", "path": args.get("path")}

    def execute(self, context, args):
        raw_path = args.get("path")
        if not raw_path:
            raise ToolError("missing_argument", "path 参数必填")
        target = context.access.workspace_path(raw_path, must_exist=True)
        if not target.is_file():
            raise ToolError("invalid_path", "path 必须是文件")
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError) as exc:
            raise ToolError("read_error", f"无法读取文件：{exc}") from exc

        headings = []
        for m in _HEADING.finditer(text):
            headings.append({"level": len(m.group(1)), "title": m.group(2).strip()})

        paragraphs = []
        for m in _PARAGRAPH.finditer(text[:5000]):
            snippet = m.group(1).strip()[:200]
            if snippet:
                paragraphs.append(snippet)

        word_count = len(text)
        char_names = _extract_names(text)

        return {
            "path": str(target.relative_to(context.access.workspace)),
            "headings": headings[:20],
            "paragraph_summaries": paragraphs[:10],
            "word_count": word_count,
            "characters_mentioned": char_names,
        }


def _extract_names(text: str) -> list[str]:
    """简单启发式提取可能的角色名（连续中文字符或大写开头的英文词）。"""
    chinese_names = re.findall(r"(?<![，。、；：])[\u4e00-\u9fff]{2,4}(?=[说问道看向])", text[:3000])
    english_names = re.findall(r"\b[A-Z][a-z]{2,15}\b", text[:3000])
    seen = set()
    result = []
    for name in chinese_names[:10] + english_names[:10]:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result[:15]
