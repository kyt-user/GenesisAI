"""内核工具：文件名/正文检索与代码结构索引。吸收原 file_search + code_index。"""

import fnmatch
import re
import time
from pathlib import Path

from genesisai.shared.security import ToolError
from genesisai.core.extensions.tools.filesystem.shared import MAX_BYTES, extract, read_text_document, walk


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
            try:
                f = context.access.workspace_path(f, must_exist=True)
                rel = context.access.display_path(f)
                text = f.read_text(encoding="utf-8", errors="replace")
            except (ToolError, OSError, UnicodeError):
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
        if mode not in {"name", "content"}:
            raise ToolError("validation_error", "mode 必须是 name/content/index")
        try:
            matcher = re.compile(query if args.get("regex") else re.escape(query),
                                 0 if args.get("regex") else re.IGNORECASE).search
        except re.error as exc:
            raise ToolError("validation_error", f"正则表达式无效：{exc}") from exc
        offset, limit = args.get("offset", 0), args.get("limit", 30)
        radius = args.get("context_lines", 1)
        base = context.access.read(args["path"])
        paths = [base] if base.is_file() else walk(context.access, args["path"], cap=None)
        file_offset = args.get("file_offset", 0)
        next_file = None
        pattern = args.get("glob", "*")
        excludes = args.get("exclude", [])
        hits, files, skipped = [], {}, []
        seen = 0
        incomplete = False
        more = False
        for index, path in enumerate(paths):
            if context.store.record.deadline and time.time() >= context.store.record.deadline:
                raise ToolError("timeout", "资料扫描达到运行截止时间")
            if index < file_offset:
                continue
            if index == file_offset + 1000:
                next_file = index
                incomplete = True
                break
            relative = path.relative_to(base) if path.is_relative_to(base) and base.is_dir() else Path(path.name)
            if len(relative.parts) > args.get("depth", 1000):
                continue
            if any(fnmatch.fnmatch(relative.as_posix(), item) or item in relative.parts for item in excludes):
                continue
            if not (fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(relative.as_posix(), pattern)):
                continue
            lines = []
            extracted = path.suffix.lower() in {".pdf", ".docx", ".xlsx"}
            if mode == "content":
                try:
                    if path.stat().st_size > MAX_BYTES:
                        raise ToolError("size_limit", "文件过大")
                    if extracted:
                        content, clipped = extract(path)
                        incomplete |= clipped
                    else:
                        content = read_text_document(path)[0]
                    lines = content.splitlines()
                except Exception:
                    skipped.append(str(path))
            def matches():
                for i, line in enumerate(lines):
                    match = matcher(line)
                    if match:
                        left = max(0, match.start() - 100)
                        yield {"line": i + 1, "column": match.start() + 1,
                               "kind": "extracted" if extracted else "source", "text": line[left:left + 400],
                               "before": [s[:240] for s in lines[max(0, i-radius):i]],
                               "after": [s[:240] for s in lines[i+1:i+1+radius]]}
                if matcher(path.name):
                    yield {"kind": "filename"}
            for hit in matches():
                seen += 1
                if seen <= offset:
                    continue
                if len(hits) == limit:
                    more = True
                    break
                hits.append({"path": str(path), **hit})
                files[str(path)] = {"path": str(path), "size": path.stat().st_size}
            if more:
                break
        return {"matches": hits, "files": list(files.values()), "skipped": skipped[:50],
                "scan_truncated": incomplete, "truncated": incomplete or more,
                "next_offset": offset + limit if more else None, "next_file_offset": next_file}
