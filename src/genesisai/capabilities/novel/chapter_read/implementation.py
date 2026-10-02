"""读取指定章节文件，返回正文和统计信息。"""

from pathlib import Path

from genesisai.shared.security import ToolError


class Implementation:
    def prepare(self, context, args):
        return {"name": "chapter_read", "path": args.get("path")}

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

        offset = args.get("offset", 0)
        limit = args.get("limit", 20000)
        total = len(text)
        excerpt = text[offset:offset + limit]

        lines = text.split("\n")
        word_count = len(text)
        line_count = len(lines)

        heading = ""
        for line in lines[:10]:
            stripped = line.strip()
            if stripped.startswith("#"):
                heading = stripped.lstrip("#").strip()
                break

        return {
            "path": str(target.relative_to(context.access.workspace)),
            "heading": heading,
            "total_chars": total,
            "line_count": line_count,
            "text": excerpt,
            "truncated": total > offset + limit,
            "next_offset": offset + limit if total > offset + limit else None,
        }
