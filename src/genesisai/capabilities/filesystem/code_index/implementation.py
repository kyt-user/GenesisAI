"""轻量级代码结构索引：提取类/函数签名、导入关系和 TODO 标记。"""

import re
from pathlib import Path

from genesisai.shared.security import ToolError


_PY_CLASS = re.compile(r"^(\s*)class\s+(\w+)(?:\(([^)]*)\))?\s*:", re.MULTILINE)
_PY_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)", re.MULTILINE)
_PY_IMPORT = re.compile(r"^(?:from\s+(\S+)\s+import\s+(.+)|import\s+(\S+))", re.MULTILINE)

_JS_EXPORT = re.compile(
    r"^(?:export\s+(?:default\s+)?(?:async\s+)?(?:function|class)\s+(\w+)"
    r"|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:function|\(|class))",
    re.MULTILINE,
)
_JS_IMPORT = re.compile(r"""^import\s+.*?from\s+['"]([^'"]+)['"]""", re.MULTILINE)

_TODO = re.compile(r"\b(TODO|FIXME|HACK|XXX|NOTE)\b\s*:?\s*(.*)", re.IGNORECASE)

_CODE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mjs": "javascript",
    ".cjs": "javascript",
}

_MAX_FILES = 100
_MAX_FILE_SIZE = 200_000


def _index_python(text: str, rel_path: str) -> dict:
    classes, functions, imports = [], [], []
    for m in _PY_CLASS.finditer(text):
        line = text[:m.start()].count("\n") + 1
        bases = m.group(3) or ""
        classes.append({"name": m.group(2), "bases": bases.strip(), "line": line})
    for m in _PY_DEF.finditer(text):
        line = text[:m.start()].count("\n") + 1
        sig = m.group(0).strip()
        functions.append({"name": m.group(2), "signature": sig, "line": line})
    for m in _PY_IMPORT.finditer(text):
        line = text[:m.start()].count("\n") + 1
        if m.group(1):
            imports.append({"module": m.group(1), "names": m.group(2).strip(), "line": line})
        else:
            imports.append({"module": m.group(3), "names": "*", "line": line})
    return {"classes": classes, "functions": functions, "imports": imports}


def _index_javascript(text: str, rel_path: str) -> dict:
    exports, imports = [], []
    for m in _JS_EXPORT.finditer(text):
        line = text[:m.start()].count("\n") + 1
        name = m.group(1) or m.group(2)
        if name:
            exports.append({"name": name, "line": line})
    for m in _JS_IMPORT.finditer(text):
        line = text[:m.start()].count("\n") + 1
        imports.append({"module": m.group(1), "line": line})
    return {"exports": exports, "imports": imports}


def _find_todos(text: str) -> list[dict]:
    todos = []
    for m in _TODO.finditer(text):
        line = text[:m.start()].count("\n") + 1
        todos.append({"tag": m.group(1).upper(), "note": m.group(2).strip()[:120], "line": line})
    return todos


class Implementation:
    def prepare(self, context, args):
        return {"name": "code_index", "path": args.get("path", ".")}

    def execute(self, context, args):
        raw_path = args.get("path", ".")
        target = context.access.workspace_path(raw_path, must_exist=True, allow_directory=True)
        glob_pattern = args.get("glob")
        max_depth = args.get("depth", 5)

        if target.is_file():
            files = [target]
        elif target.is_dir():
            pattern = glob_pattern or "**/*"
            files = sorted(
                p for p in target.glob(pattern)
                if p.is_file() and p.suffix in _CODE_EXTENSIONS
            )[:_MAX_FILES]
        else:
            raise ToolError("invalid_path", "路径必须是文件或目录")

        results = []
        total_todos = []
        for f in files:
            rel = str(f.relative_to(context.access.workspace))
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeError):
                continue
            if len(text) > _MAX_FILE_SIZE:
                text = text[:_MAX_FILE_SIZE]
            lang = _CODE_EXTENSIONS.get(f.suffix, "unknown")
            if lang == "python":
                index = _index_python(text, rel)
            elif lang in ("javascript", "typescript"):
                index = _index_javascript(text, rel)
            else:
                index = {}
            todos = _find_todos(text)
            total_todos.extend({"file": rel, **t} for t in todos)
            entry = {"file": rel, "language": lang, "lines": text.count("\n") + 1, **index}
            if todos:
                entry["todos"] = todos
            results.append(entry)

        summary = {
            "files_indexed": len(results),
            "total_classes": sum(len(r.get("classes", [])) for r in results),
            "total_functions": sum(len(r.get("functions", [])) for r in results),
            "total_exports": sum(len(r.get("exports", [])) for r in results),
            "total_todos": len(total_todos),
        }
        return {
            "summary": summary,
            "files": results,
            "todos": total_todos[:50],
            "truncated": len(files) >= _MAX_FILES,
        }
