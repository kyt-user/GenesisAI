"""按 ID 加载一个已选择的 Memory 正文及其验证元数据。"""

from genesisai.memory.manager import MemoryError, manager_for
from genesisai.shared.security import ToolError

class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args):
        try:
            with manager_for(context) as manager:
                return manager.read(args["id"])
        except MemoryError as exc: raise ToolError("memory_error", str(exc)) from exc



