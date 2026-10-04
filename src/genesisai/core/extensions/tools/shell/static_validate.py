"""小型本地前端项目的确定性静态检查。"""

from __future__ import annotations

import json
import sys
from html.parser import HTMLParser
from pathlib import Path


IGNORED_PARTS = frozenset({".git", ".genesis", "agent_docs", "node_modules"})
VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})


class StructureParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.tags: set[str] = set()
        self.errors: list[str] = []
        self.scripts: list[str] = []
        self._script_parts: list[str] | None = None

    def handle_decl(self, decl):
        if not decl.casefold().startswith("doctype html"):
            self.errors.append("DOCTYPE 必须声明为 html")

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        self.tags.add(tag)
        if tag not in VOID_TAGS:
            self.stack.append(tag)
        if tag == "script":
            self._script_parts = []

    def handle_startendtag(self, tag, attrs):
        self.tags.add(tag.casefold())

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag == "script" and self._script_parts is not None:
            self.scripts.append("".join(self._script_parts))
            self._script_parts = None
        if tag in VOID_TAGS:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"标签闭合顺序无效：{tag}")
            return
        self.stack.pop()

    def handle_data(self, data):
        if self._script_parts is not None:
            self._script_parts.append(data)


def _balanced_javascript(source: str) -> bool:
    pairs = {"(": ")", "[": "]", "{": "}"}
    closers = set(pairs.values())
    stack: list[str] = []
    quote = None
    escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "/" and following == "/":
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline
            continue
        elif char == "/" and following == "*":
            end = source.find("*/", index + 2)
            if end < 0:
                return False
            index = end + 2
            continue
        elif char in pairs:
            stack.append(pairs[char])
        elif char in closers:
            if not stack or stack.pop() != char:
                return False
        index += 1
    return quote is None and not stack


def validate_html_text(text: str, label: str = "index.html") -> dict:
    errors = []
    parser = StructureParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        return {"file": label, "errors": [f"{label}: HTML 解析失败（{type(exc).__name__}）"], "passed": False}
    if not text.lstrip().casefold().startswith("<!doctype html>"):
        errors.append(f"{label}: 缺少 <!doctype html>")
    for required in ("html", "body"):
        if required not in parser.tags:
            errors.append(f"{label}: 缺少 <{required}> 标签")
    if parser.stack:
        errors.append(f"{label}: 未闭合标签 {', '.join(parser.stack[-5:])}")
    errors.extend(f"{label}: {message}" for message in parser.errors)
    for number, script in enumerate(parser.scripts, 1):
        if script.strip() and not _balanced_javascript(script):
            errors.append(f"{label}: 第 {number} 个内联脚本括号或字符串未闭合")
    return {"file": label, "errors": errors, "passed": not errors}


def validate(root: Path) -> dict:
    files = []
    errors = []
    for path in sorted(root.rglob("*.htm*")):
        relative = path.relative_to(root)
        if not path.is_file() or any(part in IGNORED_PARTS for part in relative.parts):
            continue
        files.append(relative.as_posix())
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"{relative}: 不是可读 UTF-8 文本（{type(exc).__name__}）")
            continue
        errors.extend(validate_html_text(text, relative.as_posix())["errors"])
    if not files:
        errors.append("未发现 HTML 文件")
    return {"files": files, "checked": len(files), "errors": errors, "passed": not errors}


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    result = validate(root)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
