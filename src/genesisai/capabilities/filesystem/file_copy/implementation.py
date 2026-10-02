"""将授权原件逐字节复制到输出目录并登记产物。"""

from genesisai.shared.security import ToolError
from genesisai.state.store import sha
from genesisai.capabilities.filesystem.shared import MAX_BYTES


class Implementation:
    def prepare(self, context, args):
        target = context.access.write(args["path"])
        source = context.access.read(args["source"])
        if not source.is_file():
            raise ToolError("invalid_source", "复制来源必须是文件")
        if source.stat().st_size > MAX_BYTES:
            raise ToolError("size_limit", "复制文件超过 8 MiB")
        return {
            "name": "file_copy",
            "args": args,
            "target": str(target),
            "grants": context.store.data["grants"],
            "output": context.store.data["output"],
            "source_sha256": sha(source),
        }

    def execute(self, context, args):
        source = context.access.read(args["source"])
        if not source.is_file():
            raise ToolError("invalid_source", "复制来源必须是文件")
        if source.stat().st_size > MAX_BYTES:
            raise ToolError("size_limit", "复制文件超过 8 MiB")
        path = context.access.create(args["path"], source.read_bytes())
        if sha(path) != sha(source):
            raise ToolError("integrity_error", "复制文件哈希不一致")
        return {"path": str(path), "artifact_refs": [context.store.artifact(path)]}

