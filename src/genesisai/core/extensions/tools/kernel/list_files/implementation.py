"""内核工具：分页列出授权目录中的文件路径和大小。吸收原 file_list。"""

import time

from genesisai.shared.security import ToolError
from genesisai.core.extensions.tools.filesystem.shared import walk


class Implementation:
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        paths = []
        scan_truncated = False
        for index, path in enumerate(walk(context.access, args["path"], cap=1001)):
            if context.store.data["deadline"] and time.time() >= context.store.data["deadline"]:
                raise ToolError("timeout", "资料扫描达到运行截止时间")
            if index == 1000:
                scan_truncated = True
                break
            paths.append(path)
        offset = args.get("offset", 0)
        results = [{"path": str(path), "size": path.stat().st_size} for path in paths]
        return {
            "files": results[offset: offset + 100],
            "skipped": [],
            "truncated": scan_truncated or len(results) > offset + 100,
            "next_offset": offset + 100 if len(results) > offset + 100 else None,
        }