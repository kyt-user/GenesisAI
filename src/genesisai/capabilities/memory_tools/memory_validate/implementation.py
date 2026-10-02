"""重新计算项目来源哈希并标记已经失效的 Memory。"""

from genesisai.memory.manager import manager_for

class Implementation:
    def prepare(self, context, args): return {"name": "memory_validate", "target": str(context.access.workspace / ".genesis" / "memory")}
    def execute(self, context, args):
        with manager_for(context) as manager:
            changed = manager.validate_sources()
        return {"changed": changed, "count": len(changed)}



