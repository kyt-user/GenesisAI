"""按关键词和类型检索项目 Memory 摘要，不默认加载正文。"""

from genesisai.memory.manager import MEMORY_TYPES, manager_for
from genesisai.shared.security import ToolError

class Implementation:
    def prepare(self, context, args): return None
    def execute(self, context, args):
        kind = args.get("type")
        if kind and kind not in MEMORY_TYPES:
            raise ToolError("validation_error", "未知 Memory 类型")
        with manager_for(context) as manager:
            return {"entries": manager.search(args.get("query", ""), type=kind, limit=args.get("limit", 20))}



