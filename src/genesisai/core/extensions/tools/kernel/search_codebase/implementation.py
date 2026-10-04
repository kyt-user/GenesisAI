"""内核工具：文件名/正文检索与代码结构索引。吸收原 file_search + code_index。"""

import fnmatch
import re
import time
from pathlib import Path

from genesisai.shared.security import ToolError
from genesisai.core.extensions.tools.filesystem.shared import extract, walk


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
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mjs": "javascript", ".cjs": "javascript",
}
_MAX_FILES = 100
_MAX_FILE_SIZE = 200_000


def _index_python(text):
    classes, functions, imports = [], [], []
    for m in _PY_CLASS.finditer(text):
        classes.append({"name": m.group(2), "bases": (m.group(3) or "").strip(), "line": text[:m.start()].count("\n") + 1})
    for m in _PY_DEF.finditer(text):
        functions.append({"name": m.group(2), "signature": m.group(0).strip(), "line": text[:m.start()].count("\n") + 1})
    for m in _PY_IMPORT.finditer(text):
        line = text[:m.start()].count("\n") + 1
        if m.group(1):
            imports.append({"module": m.group(1), "names": m.group(2).strip(), "line": line})
        else:
            imports.append({"module": m.group(3), "names": "*", "line": line})
    return {"classes": classes, "functions": functions, "imports": imports}


def _index_javascript(text):
    exports, imports = [], []
    for m in _JS_EXPORT.finditer(text):
        name = m.group(1) or m.group(2)
        if name:
            exports.append({"name": name, "line": text[:m.start()].count("\n") + 1})
    for m in _JS_IMPORT.finditer(text):
        imports.append({"module": m.group(1), "line": text[:m.start()].count("\n") + 1})
    return {"exports": exports, "imports": imports}


def _find_todos(text):
    return [
        {"tag": m.group(1).upper(), "note": m.group(2).strip()[:120], "line": text[:m.start()].count("\n") + 1}
        for m in _TODO.finditer(text)
    ]


class Implementation:
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        mode = args.get("mode")
        query = args.get("query")
        if mode == "index" or (query is None and mode is None):
            return self._index(context, args)
        if query is None:
            raise ToolError("validation_error", "检索需要提供 query")
        return self._search(context, args, query, mode or "content")

    def _index(self, context, args):
        target = context.access.workspace_path(args["path"], must_exist=True, allow_directory=True)
        glob_pattern = args.get("glob")
        if target.is_file():
            files = [target]
        elif target.is_dir():
            files = sorted(p for p in target.glob(glob_pattern or "**/*") if p.is_file() and p.suffix in _CODE_EXTENSIONS)[:_MAX_FILES]
        else:
            raise ToolError("invalid_path", "路径必须是文件或目录")
        results, total_todos = [], []
        for f in files:
            rel = str(f.relative_to(context.access.workspace))
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeError):
                continue
            if len(text) > _MAX_FILE_SIZE:
                text = text[:_MAX_FILE_SIZE]
            lang = _CODE_EXTENSIONS.get(f.suffix, "unknown")
            index = _index_python(text) if lang == "python" else (_index_javascript(text) if lang in ("javascript", "typescript") else {})
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
        return {"summary": summary, "files": results, "todos": total_todos[:50], "truncated": len(files) >= _MAX_FILES}

    def _search(self, context, args, query, mode):
        results, skipped, paths = [], [], []
        scan_truncated = False
        base = context.access.read(args["path"])
        pattern = args.get("glob", "*")
        excludes = args.get("exclude", [".git", ".venv", "node_modules", "__pycache__"])
        matcher = (lambda value: re.search(query, value) is not None) if args.get("regex") else (lambda value: query.casefold() in value.casefold())
        for index, path in enumerate(walk(context.access, args["path"], cap=1001)):
            if context.store.data["deadline"] and time.time() >= context.store.data["deadline"]:
                raise ToolError("timeout", "资料扫描达到运行截止时间")
            if index == 1000:
                scan_truncated = True
                break
            paths.append(path)
            relative = path.relative_to(base) if base.is_dir() else Path(path.name)
            relative_text = str(relative).replace("\\", "/")
            if any(fnmatch.fnmatch(relative_text, item) or item in relative.parts for item in excludes):
                continue
            if not fnmatch.fnmatch(path.name, pattern) and not fnmatch.fnmatch(relative_text, pattern):
                continue
            match = matcher(path.name)
            if mode == "content":
                try:
                    text, _ = extract(path)
                    match = match or matcher(text)
                except Exception:
                    skipped.append(str(path))
            if match:
                results.append({"path": str(path), "size": path.stat().st_size})
        offset = args.get("offset", 0)
        return {
            "files": results[offset: offset + 100],
            "skipped": skipped[:50],
            "truncated": scan_truncated or len(results) > offset + 100,
            "next_offset": offset + 100 if len(results) > offset + 100 else None,
        }