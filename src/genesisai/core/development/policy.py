"""开发验证策略：按实际变更驱动验证，无需修改的结论使用事实验收。

从 `agent/loop.py` 拆出（规格 §6），属 L4 core。agent 只持有端口并传入自身，
不 import 任何 core.*；app 在组装期构造并挂到 `runtime.development`。
"""

from __future__ import annotations

import json
from pathlib import Path

from genesisai.agent.state_machine import StateTransitionError
from genesisai.shared.ids import uid


class DevelopmentPolicy:
    """开发验证策略（从 Runner 拆出，属 L4 core）。"""

    def __init__(self):
        self.verifiers = {}

    def register_verifier(self, name, planner):
        if name in self.verifiers or not callable(planner):
            raise ValueError("Verifier requires a unique name and callable")
        self.verifiers[name] = planner

    def should_auto_verify(self, state: dict) -> bool:
        """判断当前状态是否满足自动验证条件。"""
        if state.get("pending_verification_jobs"):
            return False
        profile = state.get("profile", "")
        protocols = state.get("protocols", [])
        if profile != "local_files" and "coding" not in protocols:
            return False
        return state.get("change_revision", 0) > state.get("verified_revision", -1)

    def requires_change_verification(
        self, result: dict, *, run_state: dict | None = None
    ) -> bool:
        """代码变更需要验证；普通文档由实际验收项决定是否升级。"""
        data = result.get("data") or {}
        state = run_state or {}
        if "requires_verification" in data:
            return bool(data["requires_verification"])
        paths = [item["path"] for item in result.get("observations", []) if item["kind"] == "resource_changed"]
        if paths and all(Path(path).suffix.casefold() in {".md", ".rst", ".adoc", ".txt", ".csv", ".docx", ".pdf", ".pptx", ".xlsx"} for path in paths):
            return False
        protocols = state.get("protocols", [])
        return (
            state.get("profile") == "local_files"
            or "coding" in protocols
            or "testing" in protocols
        )

    def inject_verification_test(self, runner):
        """代码修改后注入自动测试调用进行验证。"""
        d = runner.store.record
        state = d.run_runtime
        available = {item["name"] for items in runner.runtime.inventory().values() for item in items if item["enabled"]}
        selected = None
        for planner in self.verifiers.values():
            selected = planner(d.workspace, state, available)
            if selected:
                break
        if selected is None:
            runner._needs_verification = False
            return runner._request_repair("acceptance",
                "变更尚缺验收证据。请按改动风险选择针对性测试、构建或静态检查；run_commands 的 verification=build/static 可记录实际命令结果。普通文档使用读取/差异核对并关联验收项，不要求运行项目全套测试。",
                identity="verification_selection:" + str(state.get("change_revision", 0)))
        if selected["name"] not in available:
            raise ValueError("Verifier selected an unavailable tool")
        state["verified_revision"] = state.get("change_revision", 0)
        state["verification_rounds"] = state.get("verification_rounds", 0) + 1
        runner._needs_verification = False
        synthetic_call = {
            "id": uid("call"),
            "name": selected["name"],
            "arguments": json.dumps(selected["arguments"]),
        }
        d.messages.append(
            dict(
                role="assistant",
                content="[项目验证] 正在执行项目注册的验证方法…",
                reasoning="",
                tool_calls=[synthetic_call],
            )
        )
        d.pending = [synthetic_call]
        if runner.machine.state not in {"execute"}:
            try:
                runner.machine.transition("execute", "auto_verification")
            except StateTransitionError:
                pass
        runner.store.commit()
        runner.on_event("verification_start", {"round": state["verification_rounds"]})
        return None
