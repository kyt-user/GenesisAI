"""按文件名或受支持文件的正文搜索授权目录。"""

import fnmatch
import time
from pathlib import Path

from genesisai.shared.security import ToolError
from genesisai.capabilities.filesystem.shared import extract, walk


class Implementation:
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        results, skipped = [], []
        paths = []
        scan_truncated = False
        query = args["query"].casefold()
        base = context.access.read(args["path"])
        pattern = args.get("glob", "*")
        excludes = args.get("exclude", [".git", ".venv", "node_modules", "__pycache__"])
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
            match = query in path.name.casefold()
            if args.get("content"):
                try:
                    text, _ = extract(path)
                    match = match or query in text.casefold()
                except Exception:
                    skipped.append(str(path))
            if match:
                results.append({"path": str(path), "size": path.stat().st_size})
        offset = args.get("offset", 0)
        return {
            "files": results[offset : offset + 100],
            "skipped": skipped[:50],
            "truncated": scan_truncated or len(results) > offset + 100,
            "next_offset": offset + 100 if len(results) > offset + 100 else None,
        }

