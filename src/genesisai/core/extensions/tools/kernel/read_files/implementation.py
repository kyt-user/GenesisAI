"""内核工具：读取一个或多个授权文件正文，支持行范围或偏移分页。

吸收原 file_read（多文件 + 分页）。
"""

from genesisai.shared.security import ToolError
from genesisai.shared.ids import sha

from genesisai.core.extensions.tools.filesystem.shared import extract
from genesisai.core.extensions.tools.filesystem import text_ranges


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
                body, info, requested_end, next_character = text_ranges.lines(path, line_start, line_end, remaining)
                if next_character is not None:
                    if requested_end < line_start:
                        remaining_requests.extend([{"path": request["path"], "offset": next_character, "limit": request.get("limit", 8000)}, *targets[index+1:]])
                        break
                    remaining_requests.append({**request, "line_start": requested_end + 1})
                truncated = requested_end < info["total_lines"]
                entry = {"path": str(path), "text": body, "sha256": digest,
                         **info, "line_start": line_start, "line_end": requested_end,
                         "next_offset": None, "truncated": truncated}
            else:
                if path.suffix.lower() in {".pdf", ".docx", ".xlsx", ".pptx"}:
                    from genesisai.core.extensions.tools.office.readers import text_page
                    body, info, next_offset = text_page(path, path.suffix.lower()[1:], offset, limit)
                    clipped = False
                else:
                    body, info, next_offset = text_ranges.characters(path, offset, limit)
                    clipped = False
                truncated = clipped or next_offset is not None
                entry = {"path": str(path), "text": body, "sha256": digest, "offset": offset,
                         **info, "next_offset": next_offset, "truncated": truncated}
                if next_offset is not None:
                    remaining_requests.append({**request, "offset": next_offset})
            refs.append(context.store.source(kind="file", path=str(path), sha256=digest, title=path.name))
            files.append(entry)
            truncated_any = truncated_any or truncated
            remaining -= len(body)
        return {"files": files, "source_refs": refs, "truncated": truncated_any or bool(remaining_requests),
                "remaining_requests": remaining_requests}
