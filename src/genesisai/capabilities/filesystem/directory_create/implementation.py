"""在工作区内创建新目录，并拒绝覆盖现有路径。"""

from genesisai.shared.changes import record_change
from genesisai.shared.security import ToolError


class Implementation:
    def _target(self, context, args):
        target = context.access.workspace_path(args["path"])
        if target.exists():
            raise ToolError("target_exists", "目标路径已经存在")
        return target

    def prepare(self, context, args):
        target = self._target(context, args)
        return {"name": "directory_create", "target": str(target)}

    def execute(self, context, args):
        target = self._target(context, args)
        target.mkdir(parents=True, exist_ok=False)
        change = record_change(context.store, operation="mkdir", path=target)
        return {"path": str(target), "created": True, "change_id": change["id"]}

