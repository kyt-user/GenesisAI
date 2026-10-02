"""将指定 Memory 标记为已遗忘并保留可撤销审计记录。"""

from genesisai.memory.manager import MemoryError, manager_for
from genesisai.shared.security import ToolError

class Implementation:
    def prepare(self, context, args): return {"name": "memory_forget", "target": args["id"]}
    def execute(self, context, args):
        try:
            with manager_for(context) as manager:
                return manager.forget(args["id"], run_id=context.store.data.get("run_id"))
        except MemoryError as exc: raise ToolError("memory_error", str(exc)) from exc



