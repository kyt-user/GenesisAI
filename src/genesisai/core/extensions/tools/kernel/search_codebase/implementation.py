"""内核工具：文件名/正文检索与代码结构索引。吸收原 file_search + code_index。"""

import fnmatch
import hashlib
import json
import re
import time
from collections import deque
from contextlib import ExitStack
from pathlib import Path

from genesisai.shared.security import ToolError
from genesisai.core.extensions.tools.filesystem.shared import extract, read_text_document, walk
from genesisai.core.extensions.tools.filesystem import text_ranges
from genesisai.core.memory.project_index import python_structure


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


def _index_python(text):
    try:
        structure = python_structure(text)
    except (SyntaxError, ValueError) as exc:
        return {"classes": [], "functions": [], "imports": [], "coverage": "syntax_error", "parse_error": str(exc)[:500]}
    return {"classes": [item for item in structure["symbols"] if item["kind"] == "ClassDef"],
            "functions": [item for item in structure["symbols"] if item["kind"] != "ClassDef"],
            "imports": [{"module": name} for name in structure["imports"]],
            "coverage": structure["coverage"]}


def _index_javascript(text):
    exports, imports = [], []
    for m in _JS_EXPORT.finditer(text):
        name = m.group(1) or m.group(2)
        if name:
            exports.append({"name": name, "line": text[:m.start()].count("\n") + 1})
    for m in _JS_IMPORT.finditer(text):
        imports.append({"module": m.group(1), "line": text[:m.start()].count("\n") + 1})
    return {"exports": exports, "imports": imports, "coverage": "regex"}


def _find_todos(text):
    return [
        {"tag": m.group(1).upper(), "note": m.group(2).strip(), "line": number}
        for number, line in enumerate(text.splitlines(), 1) for m in _TODO.finditer(line)
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
            files = []
            for p in walk(context.access, str(target), cap=None):
                if context.store.record.deadline and time.time() >= context.store.record.deadline:
                    raise ToolError("timeout", "索引扫描达到任务截止时间")
                rel = p.relative_to(target)
                if p.suffix not in _CODE_EXTENSIONS or len(rel.parts) > args.get("depth", 1000):
                    continue
                if glob_pattern and not (p.match(glob_pattern) or fnmatch.fnmatch(rel.as_posix(), glob_pattern)):
                    continue
                if any(fnmatch.fnmatch(rel.as_posix(), pattern) or pattern in rel.parts for pattern in args.get("exclude", [])):
                    continue
                files.append(p)
            files.sort()
        else:
            raise ToolError("invalid_path", "路径必须是文件或目录")
        snapshot = hashlib.sha256(json.dumps([
            str(target), [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in files]
        ], ensure_ascii=False).encode()).hexdigest()
        offset = args.get("file_offset", 0)
        item_offset = 0
        if args.get("cursor"):
            try:
                cursor = json.loads(args["cursor"])
                if type(cursor.get("offset")) is not int or cursor["offset"] < 0:
                    raise ValueError()
                offset = cursor["offset"]
                item_offset = cursor.get("item_offset", 0)
                if type(item_offset) is not int or item_offset < 0:
                    raise ValueError()
            except (ValueError, AttributeError, TypeError) as exc:
                raise ToolError("validation_error", "索引 cursor 无效，请使用 next_cursor 原值") from exc
            if cursor.get("snapshot") != snapshot:
                raise ToolError("stale_cursor", "文件集合或内容已变化；请不带 cursor 重新开始索引，避免遗漏或重复")
        results, total_todos, skipped = [], [], []
        remaining = args.get("max_output_chars", 24000)
        next_offset = offset
        partial_files = False
        for f in files[offset:offset + args.get("limit", 100)]:
            try:
                f = context.access.workspace_path(f, must_exist=True)
                rel = context.access.display_path(f)
                text = read_text_document(f)[0]
            except (ToolError, OSError, UnicodeError) as exc:
                skipped.append({"path": str(f), "reason": str(exc)})
                next_offset += 1
                continue
            total_lines = text.count("\n") + 1
            lang = _CODE_EXTENSIONS.get(f.suffix, "unknown")
            index = _index_python(text) if lang == "python" else (_index_javascript(text) if lang in ("javascript", "typescript") else {})
            todos = _find_todos(text)
            entry = {"file": rel, "language": lang, "lines": total_lines, "indexed_characters": len(text), "content_truncated": False, **index}
            if todos:
                entry["todos"] = todos
            size = len(json.dumps(entry, ensure_ascii=False))
            if size > remaining and results:
                break
            # Page symbols within a file, retaining the exact continuation position.
            values = [(key, value) for key, items in entry.items() if isinstance(items, list) for value in items]
            entry = {key: ([] if isinstance(value, list) else value) for key, value in entry.items()}
            entry.update(item_offset=item_offset, total_items=len(values))
            emitted = 0
            for key, value in values[item_offset:]:
                candidate_size = len(json.dumps(value, ensure_ascii=False))
                size = len(json.dumps(entry, ensure_ascii=False))
                if size + candidate_size > remaining and emitted:
                    break
                entry[key].append(value)
                emitted += 1
            entry["symbols_truncated"] = item_offset + emitted < len(values)
            size = len(json.dumps(entry, ensure_ascii=False))
            partial_files |= entry["symbols_truncated"] or bool(entry.get("parse_error"))
            total_todos.extend({"file": rel, **t} for t in entry.get("todos", []))
            results.append(entry)
            remaining -= size
            if entry["symbols_truncated"]:
                item_offset += emitted
                break
            next_offset += 1
            item_offset = 0
        summary = {
            "files_indexed": len(results),
            "total_classes": sum(len(r.get("classes", [])) for r in results),
            "total_functions": sum(len(r.get("functions", [])) for r in results),
            "total_exports": sum(len(r.get("exports", [])) for r in results),
            "total_todos": len(total_todos),
        }
        more = next_offset < len(files)
        return {"summary": summary, "files": results, "todos": total_todos, "skipped": skipped,
                "total_files": len(files), "file_offset": offset, "snapshot": snapshot,
                "truncated": more or partial_files or bool(skipped), "partial_files": partial_files,
                "next_file_offset": next_offset if more else None,
                "next_cursor": json.dumps({"snapshot": snapshot, "offset": next_offset, "item_offset": item_offset}) if more else None}

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
        output_mode = args.get("output_mode", "content")
        remaining = args.get("max_output_chars", 24000)
        consumed = 0
        seen = 0
        incomplete = False
        more = False
        for index, path in enumerate(paths):
            if context.store.record.deadline and time.time() >= context.store.record.deadline:
                raise ToolError("timeout", "资料扫描达到运行截止时间")
            if index < file_offset:
                continue
            relative = path.relative_to(base) if path.is_relative_to(base) and base.is_dir() else Path(path.name)
            if len(relative.parts) > args.get("depth", 1000):
                continue
            if any(fnmatch.fnmatch(relative.as_posix(), item) or item in relative.parts for item in excludes):
                continue
            if not (fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(relative.as_posix(), pattern)):
                continue
            extracted = path.suffix.lower() in {".pdf", ".docx", ".xlsx"}
            def matches():
                file_hits = 0
                if mode == "content":
                    with ExitStack() as stack:
                        try:
                            if extracted:
                                content, _ = extract(path)
                                source_lines = iter(content.splitlines(keepends=True))
                            else:
                                info = text_ranges.metadata(path)
                                source_lines = stack.enter_context(path.open(encoding=info["encoding"], newline=""))
                        except (ToolError, OSError, UnicodeError, ValueError):
                            skipped.append(str(path))
                            source_lines = iter(())
                        previous, upcoming = deque(maxlen=radius), deque()
                        upcoming.extend(next(source_lines, None) for _ in range(radius + 1))
                        number, char_offset = 0, 0
                        while upcoming and upcoming[0] is not None:
                            raw = upcoming.popleft()
                            line = raw.rstrip("\r\n")
                            number += 1
                            match = matcher(line)
                            if match:
                                file_hits += 1
                                if output_mode == "files":
                                    yield {"kind": "file"}
                                    return
                                if output_mode == "content":
                                    left = max(0, match.start() - 100)
                                    yield {"line": number, "column": match.start() + 1,
                                           "kind": "extracted" if extracted else "source", "text": line[left:left + 400],
                                           "before": [s[:240] for s in previous],
                                           "after": [s.rstrip("\r\n")[:240] for s in upcoming if s is not None],
                                           "read_request": {"path": str(path), "offset": char_offset + left, "limit": 8000}}
                            previous.append(line)
                            char_offset += len(raw)
                            upcoming.append(next(source_lines, None))
                if output_mode == "count" and file_hits:
                    yield {"kind": "count", "matching_lines": file_hits}
                    return
                if matcher(path.name):
                    yield {"kind": "filename"}
            for hit in matches():
                seen += 1
                if seen <= offset:
                    continue
                entry = {"path": str(path), **hit}
                size = len(json.dumps(entry, ensure_ascii=False))
                if len(hits) == limit or (size > remaining and hits):
                    more = True
                    break
                if size > remaining:
                    entry["before"], entry["after"] = [], []
                    entry["context_truncated"] = True
                    size = len(json.dumps(entry, ensure_ascii=False))
                hits.append(entry)
                remaining -= size
                consumed += 1
                files[str(path)] = {"path": str(path), "size": path.stat().st_size}
            if more:
                break
        return {"matches": hits, "files": list(files.values()), "skipped": skipped,
                "scan_truncated": incomplete, "truncated": incomplete or more,
                "output_mode": output_mode,
                "next_offset": offset + consumed if more else None, "next_file_offset": next_file}
