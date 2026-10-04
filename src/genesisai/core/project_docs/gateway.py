"""面向 Agent 的项目工作记忆端口实现。"""

from __future__ import annotations

from pathlib import Path

from genesisai.core.project_docs.manager import AgentDocsManager


class ProjectDocsGateway:
    """把 Store 的工作区绑定到 AgentDocsManager，供 agent 经端口调用。"""

    def __init__(self, store):
        self.store = store

    @property
    def workspace(self) -> Path:
        return Path(self.store.data.get("workspace") or getattr(self.store, "workspace", ""))

    def exists(self) -> bool:
        return AgentDocsManager.exists(self.workspace)

    def manager(self) -> AgentDocsManager:
        return AgentDocsManager(self.workspace)

    def begin_task(self, objective, *, run_id=None) -> dict:
        return self.manager().begin_task(objective, run_id=run_id)

    def record_tool(self, name: str, result: dict, args: dict) -> None:
        self.manager().record_tool(name, result, args)

    def active_task(self) -> dict | None:
        return self.manager().active_task()

    def finish_run(self, status: str, answer: str = "") -> dict | None:
        return self.manager().finish_run(status, answer)

    def finish(self, status: str, answer: str = "") -> None:
        """运行收尾：更新任务状态并写回 run_runtime（原 agent._finish_project_docs）。"""
        if not self.exists():
            return
        try:
            task = self.finish_run(status, answer)
            if task:
                self.store.data['run_runtime']['project_task_id'] = task['id']
                self.store.data['run_runtime']['project_task_status'] = task['status']
        except Exception as exc:
            self.store.data['run_runtime']['agent_docs_error'] = f'{type(exc).__name__}: {exc}'