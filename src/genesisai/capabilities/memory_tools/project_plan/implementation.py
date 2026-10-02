"""为 agent_docs 当前开发任务写入实施步骤、验收条件和非目标。"""

from genesisai.project_docs import AgentDocsError, AgentDocsManager
from genesisai.shared.security import ToolError


class Implementation:
    def prepare(self, context, args):
        manager = AgentDocsManager(context.access.workspace)
        if not manager.available:
            raise ToolError("agent_docs_unavailable", "工作区尚未初始化 agent_docs")
        task = manager.active_task()
        if not task:
            raise ToolError("project_task_missing", "当前没有活动开发任务")
        return {
            "name": "project_plan",
            "target": str(manager.root / "tasks" / task["id"] / "plan.md"),
            "steps": len(args["steps"]),
            "acceptance": len(args["acceptance"]),
        }

    def execute(self, context, args):
        manager = AgentDocsManager(context.access.workspace)
        try:
            task = manager.set_plan(
                steps=args["steps"],
                acceptance=args["acceptance"],
                non_goals=args.get("non_goals", []),
            )
        except AgentDocsError as exc:
            raise ToolError("agent_docs_error", str(exc)) from exc
        return {
            "task_id": task["id"],
            "status": task["status"],
            "plan_ready": task["plan_ready"],
            "path": str(manager.root / "tasks" / task["id"] / "plan.md"),
        }
