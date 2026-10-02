"""删除工作区内单个文件，删除前保存可撤销备份并逐次确认。"""

from genesisai.shared.changes import backup_file, current_sha, record_change
from genesisai.shared.security import ToolError


class Implementation:
    def _target(self, context, args):
        target = context.access.workspace_path(args["path"], must_exist=True)
        digest = current_sha(target)
        if args.get("expected_sha256") and args["expected_sha256"].casefold() != digest:
            raise ToolError("file_changed", "文件哈希已经变化")
        return target, digest

    def prepare(self, context, args):
        target, digest = self._target(context, args)
        return {"name": "file_delete", "target": str(target), "sha256": digest, "destructive": True}

    def execute(self, context, args):
        target, digest = self._target(context, args)
        backup = backup_file(context.store, target)
        target.unlink()
        change = record_change(context.store, operation="delete", path=target, before_sha=digest, backup=backup)
        return {"path": str(target), "deleted": True, "change_id": change["id"]}

