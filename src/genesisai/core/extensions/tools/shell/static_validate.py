"""小型本地前端项目的确定性静态检查。"""

from __future__ import annotations

import json
import sys
from html.parser import HTMLParser
from pathlib import Path
from genesisai.shared.security import ToolError


from genesisai.core.extensions.tools.shell.python_support import project_files, _read_text, _cancel
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
        if index % 65536 == 0:
            _cancel()
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
        for offset in range(0, len(text), 65536):
            _cancel()
            parser.feed(text[offset:offset + 65536])
        parser.close()
    except ToolError:
        raise
    except Exception as exc:
        return {"file": label, "errors": [f"{label}: HTML 解析失败（{type(exc).__name__}）"], "passed": False}
    if not text.lstrip().casefold().startswith("<!doctype html>"):
        errors.append(f"{label}: 缺少 <!doctype html>")
    for required in ("html", "body"):
        if required not in parser.tags:
            errors.append(f"{label}: 缺少 <{required}> 标签")
    if parser.stack:
        errors.append(f"{label}: 未闭合标签 {', '.join(parser.stack)}")
    errors.extend(f"{label}: {message}" for message in parser.errors)
    for number, script in enumerate(parser.scripts, 1):
        if script.strip() and not _balanced_javascript(script):
            errors.append(f"{label}: 第 {number} 个内联脚本括号或字符串未闭合")
    return {"file": label, "errors": errors, "passed": not errors}


def validate(root: Path) -> dict:
    root = root.resolve()
    inventory, coverage = project_files(root)
    files = [name for name in inventory if Path(name).suffix.lower() in {'.htm', '.html'}]
    errors = []
    for name in files:
        _cancel()
        text = _read_text(root, name, coverage)
        if text is not None:
            errors.extend(validate_html_text(text, name)['errors'])
    errors.extend(f"{item['path']}: {item['operation']}失败（{item['error']}）" for item in coverage['errors'])
    if not files:
        errors.append('未发现 HTML 文件')
    return {'files': files, 'checked': len(files), 'errors': errors, 'passed': not errors,
            'coverage': {key: value for key, value in coverage.items() if key != 'directories'}}


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    result = validate(root)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
