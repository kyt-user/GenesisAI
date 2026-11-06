"""内核工具：读取一个或多个授权文件正文，支持行范围或偏移分页。

吸收原 file_read（多文件 + 分页）。
"""

from genesisai.shared.security import ToolError
from genesisai.shared.ids import sha

from genesisai.core.extensions.tools.filesystem.shared import extract, read_text_document


class Implementation:
    @staticmethod
    def observe(args, payload):
        from genesisai.core.tools.observations import Observation
        return [Observation('resource_read', path=f['path'], sha256=f['sha256']) for f in payload.get('files', [])]

    def prepare(self, context, args):
        return None

    @staticmethod
    def _targets(args):
        if args.get("files"):
            if args.get("path") or args.get("paths"):
                raise ToolError("validation_error", "files 不能与 path/paths 混用")
            return args["files"]
        targets = list(args.get("paths") or [])
        if args.get("path"):
            targets.append(args["path"])
        if not targets:
            raise ToolError("validation_error", "必须提供 path 或 paths")
        if len(targets) > 50:
            raise ToolError("validation_error", "一次最多读取 50 个文件")
        return [{"path": path, **{key: args[key] for key in ("line_start", "line_end", "offset", "limit") if key in args}} for path in targets]

    def execute(self, context, args):
        targets = self._targets(args)
        for request in targets:
            start, end = request.get("line_start"), request.get("line_end")
            if (start is None) != (end is None) or (start is not None and end < start):
                raise ToolError("validation_error", "每个文件的 line_start 和 line_end 必须同时提供且范围有效")
            if start is not None and "offset" in request:
                raise ToolError("validation_error", "行范围不能与 offset 混用")
        remaining = args.get("max_output_chars", 24000)
        remaining_requests = []
        files, refs, truncated_any = [], [], False
        for index, request in enumerate(targets):
            if remaining <= 0:
                remaining_requests.extend(targets[index:])
                break
            line_start, line_end = request.get("line_start"), request.get("line_end")
            offset, limit = request.get("offset", 0), min(request.get("limit", 8000), remaining)
            path = context.access.read(request["path"])
            digest = sha(path)
            if line_start is not None:
                text, encoding, newline, bom = read_text_document(path)
                lines = text.splitlines()
                selected = lines[line_start - 1:line_end]
                numbered = [f"{number}: {value}" for number, value in enumerate(selected, line_start)]
                body = "\n".join(numbered)
                requested_end = min(line_end, len(lines))
                if len(body) > remaining:
                    count, used = 0, 0
                    for line in numbered:
                        size = len(line) + bool(count)
                        if used + size > remaining:
                            break
                        used += size
                        count += 1
                    if not count:
                        # 保留整行语义；长行改用原文字符偏移继续读取。
                        raw_offset = sum(len(line) for line in text.splitlines(keepends=True)[:line_start-1])
                        remaining_requests.extend([{"path": request["path"], "offset": raw_offset, "limit": request.get("limit", 8000)}, *targets[index+1:]])
                        break
                    body = "\n".join(numbered[:count])
                    requested_end = line_start + count - 1
                    remaining_requests.append({**request, "line_start": requested_end + 1})
                truncated = requested_end < len(lines)
                entry = {
                    "path": str(path),
                    "text": body,
                    "sha256": digest,
                    "encoding": encoding,
                    "newline": "CRLF" if newline == "\r\n" else "LF",
                    "bom": bom,
                    "line_start": line_start,
                    "line_end": requested_end,
                    "total_lines": len(lines),
                    "next_offset": None,
                    "truncated": truncated,
                }
            else:
                if path.suffix.lower() in {".pdf", ".docx", ".xlsx"}:
                    text, truncated = extract(path)
                else:
                    text, _, _, _ = read_text_document(path)
                    truncated = False
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
                if entry["next_offset"] is not None:
                    remaining_requests.append({**request, "offset": entry["next_offset"]})
            refs.append(context.store.source(kind="file", path=str(path), sha256=digest, title=path.name))
            files.append(entry)
            truncated_any = truncated_any or truncated
            remaining -= len(body)
        return {"files": files, "source_refs": refs, "truncated": truncated_any or bool(remaining_requests),
                "remaining_requests": remaining_requests}
