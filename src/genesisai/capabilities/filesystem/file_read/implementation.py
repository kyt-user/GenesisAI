"""分段读取文件正文并登记可引用的本地来源。"""

from genesisai.shared.security import ToolError
from genesisai.state.store import sha
from genesisai.capabilities.filesystem.shared import extract, read_text_document


class Implementation:
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        path = context.access.read(args["path"])
        line_start = args.get("line_start")
        line_end = args.get("line_end")
        if line_start is not None or line_end is not None:
            if line_start is None or line_end is None or line_end < line_start:
                raise ToolError("validation_error", "line_start 和 line_end 必须同时提供且范围有效")
            text, encoding, newline, bom = read_text_document(path)
            lines = text.splitlines()
            selected = lines[line_start - 1:line_end]
            body = "\n".join(f"{index}: {value}" for index, value in enumerate(selected, line_start))
            truncated = line_end < len(lines)
            metadata = {
                "encoding": encoding,
                "newline": "CRLF" if newline == "\r\n" else "LF",
                "bom": bom,
                "line_start": line_start,
                "line_end": min(line_end, len(lines)),
                "total_lines": len(lines),
                "next_offset": None,
            }
        else:
            text, truncated = extract(path)
            offset, limit = args.get("offset", 0), args.get("limit", 8000)
            body = text[offset: offset + limit]
            metadata = {
                "offset": offset,
                "next_offset": offset + limit if len(text) > offset + limit else None,
            }
        ref = context.store.source(kind="file", path=str(path), sha256=sha(path), title=path.name)
        return {
            "text": body,
            "source_refs": [ref],
            "sha256": sha(path),
            "truncated": truncated or (
                line_start is None and len(text) > args.get("offset", 0) + args.get("limit", 8000)
            ),
            **metadata,
        }

