"""写入用户明确要求记住或具有可靠来源的项目 Memory。"""

from genesisai.memory.manager import MemoryError, manager_for
from genesisai.shared.security import ToolError

class Implementation:
    def prepare(self, context, args): return {"name": "memory_write", "target": str(context.access.workspace / ".genesis" / "memory"), "args": {"type": args["type"], "title": args["title"]}}
    def execute(self, context, args):
        try:
            with manager_for(context) as manager:
                return manager.add(**args, run_id=context.store.data.get("run_id"))
        except MemoryError as exc: raise ToolError("memory_error", str(exc)) from exc



