"""检查章节内容的角色名一致性。"""

import re
from pathlib import Path

from genesisai.shared.security import ToolError


class Implementation:
    def prepare(self, context, args):
        return {"name": "consistency_check", "path": args.get("path")}

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

        warnings = []
        # 直接从文本抽取候选角色名（不再依赖长期记忆库）。
        mentioned = _extract_proper_nouns(text)
        for name in mentioned:
            # 查找文本中相似但不同的拼写。
            for variant in _find_name_variants(name, text):
                warnings.append({
                    "type": "name_variant",
                    "character": name,
                    "found": variant,
                    "message": f"角色 {name} 在文本中可能以变体 {variant} 出现",
                })

        return {
            "path": str(target.relative_to(context.access.workspace)),
            "names_checked": len(mentioned),
            "warnings": warnings[:20],
            "consistent": len(warnings) == 0,
        }


def _find_name_variants(name: str, text: str) -> list[str]:
    """查找角色名的潜在拼写错误或变体。"""
    if not name or len(name) < 2:
        return []
    # 中文名字（2-4 字），查找部分匹配。
    if all('\u4e00' <= c <= '\u9fff' for c in name):
        # 查找在对话标记附近出现的单字子集。
        pattern = re.compile(f"[{re.escape(name)}]{{1}}(?=[说问道看向])")
        matches = {m.group() for m in pattern.finditer(text[:5000])}
        return [m for m in matches if m != name and len(m) >= 1]
    return []


def _extract_proper_nouns(text: str) -> list[str]:
    """从文本中提取潜在专有名词。"""
    # 中文：对话标记前的 2-4 字序列。
    chinese = re.findall(r"([\u4e00-\u9fff]{2,4})(?=[说问道看向])", text[:5000])
    # 英文：大写开头的词。
    english = re.findall(r"\b([A-Z][a-z]{2,15})\b", text[:5000])
    seen = set()
    result = []
    for name in chinese + english:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result[:20]
