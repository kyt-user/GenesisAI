"""内核工具：分页列出授权目录中的文件路径和大小。吸收原 file_list。"""

import time

from genesisai.shared.security import ToolError
from genesisai.core.extensions.tools.filesystem.shared import walk


class Implementation:
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        paths = []
        offset, limit = args.get("offset", 0), args.get("limit", 100)
        more = False
        for index, path in enumerate(walk(context.access, args["path"], include=args.get("include", []))):
            if context.store.record.deadline and time.time() >= context.store.record.deadline:
                raise ToolError("timeout", "资料扫描达到运行截止时间")
            if index < offset:
                continue
            if len(paths) == limit:
                more = True
                break
            paths.append(path)
        results = [{"path": str(path), "size": path.stat().st_size} for path in paths]
        return {
            "files": results,
            "skipped": [],
            "truncated": more,
            "next_offset": offset + len(results) if more else None,
        }
