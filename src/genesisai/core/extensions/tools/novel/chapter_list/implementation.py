"""扫描工作区章节文件，按编号排列并显示标题和字数。"""

import re
from pathlib import Path

from genesisai.shared.security import ToolError

_CHAPTER_PATTERN = re.compile(
    r"(?:chapter|chap|第)\s*(\d+)[\s._\-]*(.*)",
    re.IGNORECASE,
)
_HEADING_PATTERN = re.compile(r"^#+\s+(.+)", re.MULTILINE)
_FILE_EXTENSIONS = {".md", ".txt", ".markdown"}


def _extract_chapter_info(filepath: Path) -> dict:
    stem = filepath.stem
    match = _CHAPTER_PATTERN.search(stem)
    number = int(match.group(1)) if match else None
    title = ""
    word_count = 0
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        text = ""
    word_count = len(text)
    heading = _HEADING_PATTERN.search(text)
    if heading:
        title = heading.group(1).strip()
    elif match and match.group(2):
        title = match.group(2).strip()
    else:
        title = stem
    return {"number": number, "title": title, "words": word_count, "file": str(filepath.name)}


def _format_chapter_list(chapters: list[dict], total_words: int) -> str:
    lines = [f"找到 {len(chapters)} 章，共 {total_words} 字"]
    for c in chapters:
        num = f"第{c['number']}章" if c['number'] else "未编号"
        lines.append(f"  {num} {c['title']} ({c['words']}字) — {c['file']}")
    return "\n".join(lines)


class Implementation:
    def prepare(self, context, args):
        return {"name": "chapter_list", "path": args.get("path", ".")}

    def execute(self, context, args):
        raw_path = args.get("path", ".")
        target = context.access.workspace_path(raw_path, must_exist=True, allow_directory=True)
        if not target.is_dir():
            raise ToolError("invalid_path", "path 必须是目录")
        glob_pattern = args.get("glob", "**/*")
        files = sorted(
            p for p in target.glob(glob_pattern)
            if p.is_file() and p.suffix.lower() in _FILE_EXTENSIONS
        )
        chapters = []
        for f in files:
            info = _extract_chapter_info(f)
            info["path"] = str(f.relative_to(context.access.workspace))
            chapters.append(info)
        numbered = [c for c in chapters if c["number"] is not None]
        unnumbered = [c for c in chapters if c["number"] is None]
        numbered.sort(key=lambda c: c["number"])
        result = numbered + unnumbered
        total_words = sum(c["words"] for c in result)
        return {
            "chapter_count": len(result),
            "total_words": total_words,
            "chapters": result,
            "text": _format_chapter_list(result, total_words),
        }
