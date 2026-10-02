"""在工作区内移动或重命名文件，并拒绝覆盖现有目录。"""

import shutil

from genesisai.shared.changes import current_sha, record_change
from genesisai.shared.security import ToolError


class Implementation:
    def _paths(self, context, args):
        source = context.access.workspace_path(args["source"], must_exist=True)
        target = context.access.workspace_path(args["destination"])
        if target.exists():
            raise ToolError("target_exists", "移动目标已经存在")
        digest = current_sha(source)
        if args.get("expected_sha256") and args["expected_sha256"].casefold() != digest:
            raise ToolError("file_changed", "源文件哈希已经变化")
        return source, target, digest

    def prepare(self, context, args):
        source, target, digest = self._paths(context, args)
        return {"name": "file_move", "source": str(source), "target": str(target), "sha256": digest}

    def execute(self, context, args):
        source, target, digest = self._paths(context, args)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        change = record_change(context.store, operation="move", path=source, before_sha=digest, after_sha=None, destination=target)
        return {"source": str(source), "destination": str(target), "sha256": digest, "change_id": change["id"]}

