"""内核工具：写入 agent_docs 实施计划。

原由 memory 内核工具承载的 `operation=plan` 分支独立为 plan 工具；记忆知识库已移除。
"""

from genesisai.shared.security import ToolError
from genesisai.core.project_docs import AgentDocsError, AgentDocsManager


class Implementation:
    def prepare(self, context, args):
        manager = AgentDocsManager(context.access.workspace)
        if not manager.available:
            raise ToolError("agent_docs_unavailable", "工作区尚未初始化 agent_docs")
        task = manager.active_task()
        if not task:
            raise ToolError("project_task_missing", "当前没有活动开发任务")
        return {"name": "plan",
                "target": str(manager.root / "tasks" / task["id"] / "plan.md"),
                "steps": len(args["steps"]), "acceptance": len(args["acceptance"])}

    def execute(self, context, args):
        manager = AgentDocsManager(context.access.workspace)
        try:
            task = manager.set_plan(steps=args["steps"], acceptance=args["acceptance"], non_goals=args.get("non_goals", []))
        except AgentDocsError as exc:
            raise ToolError("agent_docs_error", str(exc)) from exc
        return {
            "task_id": task["id"],
            "status": task["status"],
            "plan_ready": task["plan_ready"],
            "path": str(manager.root / "tasks" / task["id"] / "plan.md"),
        }