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
        from genesisai.core.extensions.tools.filesystem import text_ranges
        offset = args.get('offset', 0)
        limit = min(args.get('limit', 50000), max(1, context.runtime.context_budgeter.read_page_tokens() * 3), 50000)
        excerpt, info, next_offset = text_ranges.characters(target, offset, limit)
        total, line_count = info['total_chars'], info['total_lines']
        preview, _, _ = text_ranges.characters(target, 0, min(50000, limit), info)
        lines = preview.splitlines()

        heading = ""
        for line in lines[:10]:
            stripped = line.strip()
            if stripped.startswith("#"):
                heading = stripped.lstrip("#").strip()
                break

        return {
            "path": context.access.display_path(target),
            "heading": heading,
            "total_chars": total,
            "line_count": line_count,
            "text": excerpt,
            "truncated": total > offset + limit,
            "next_offset": next_offset,
            "encoding": info["encoding"],
            "heading_is_preview": True,
        }
