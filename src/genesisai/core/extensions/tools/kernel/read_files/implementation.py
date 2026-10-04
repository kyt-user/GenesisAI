"""内核工具：读取一个或多个授权文件正文，支持行范围或偏移分页。

吸收原 file_read（多文件 + 分页）。
"""

from genesisai.shared.security import ToolError
from genesisai.core.state.store import sha
from genesisai.core.extensions.tools.filesystem.shared import extract, read_text_document


class Implementation:
    def prepare(self, context, args):
        return None

    @staticmethod
    def _targets(args):
        targets = list(args.get("paths") or [])
        if args.get("path"):
            targets.append(args["path"])
        if not targets:
            raise ToolError("validation_error", "必须提供 path 或 paths")
        if len(targets) > 50:
            raise ToolError("validation_error", "一次最多读取 50 个文件")
        return targets

    def execute(self, context, args):
        line_start = args.get("line_start")
        line_end = args.get("line_end")
        if (line_start is None) != (line_end is None):
            raise ToolError("validation_error", "line_start 和 line_end 必须同时提供")
        if line_start is not None and line_end < line_start:
            raise ToolError("validation_error", "line_start 和 line_end 必须同时提供且范围有效")
        offset, limit = args.get("offset", 0), args.get("limit", 8000)
        files, refs, truncated_any = [], [], False
        for raw in self._targets(args):
            path = context.access.read(raw)
            digest = sha(path)
            if line_start is not None:
                text, encoding, newline, bom = read_text_document(path)
                lines = text.splitlines()
                selected = lines[line_start - 1:line_end]
                body = "\n".join(f"{index}: {value}" for index, value in enumerate(selected, line_start))
                truncated = line_end < len(lines)
                entry = {
                    "path": str(path),
                    "text": body,
                    "sha256": digest,
                    "encoding": encoding,
                    "newline": "CRLF" if newline == "\r\n" else "LF",
                    "bom": bom,
                    "line_start": line_start,
                    "line_end": min(line_end, len(lines)),
                    "total_lines": len(lines),
                    "next_offset": None,
                    "truncated": truncated,
                }
            else:
                text, truncated = extract(path)
                body = text[offset: offset + limit]
                truncated = truncated or len(text) > offset + limit
                entry = {
                    "path": str(path),
                    "text": body,
                    "sha256": digest,
                    "offset": offset,
                    "next_offset": offset + limit if len(text) > offset + limit else None,
                    "truncated": truncated,
                }
            refs.append(context.store.source(kind="file", path=str(path), sha256=digest, title=path.name))
            files.append(entry)
            truncated_any = truncated_any or truncated
        return {"files": files, "source_refs": refs, "truncated": truncated_any}