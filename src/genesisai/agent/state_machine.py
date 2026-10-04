"""单 Agent Run 的确定性生命周期状态机。"""

from __future__ import annotations

import time

from genesisai.shared.run_states import RUN_STATES, TERMINAL_STATES, TRANSITIONS


class StateTransitionError(ValueError):
    """请求了不允许的 Run 状态迁移。"""


class RunStateMachine:
    def __init__(self, store):
        self.store = store

    @property
    def state(self) -> str:
        return self.store.data["run_runtime"].get("lifecycle", "ready")

    def transition(self, target: str, reason: str, *, save: bool = True) -> str:
        if target not in RUN_STATES:
            raise StateTransitionError(f"未知 Run 状态：{target}")
        current = self.state
        if target == current:
            return target
        if target not in TRANSITIONS[current]:
            raise StateTransitionError(f"不允许的 Run 状态迁移：{current} → {target}")
        runtime = self.store.data["run_runtime"]
        runtime["lifecycle"] = target
        runtime.setdefault("state_history", []).append({
            "from": current,
            "to": target,
            "reason": reason,
            "time": time.time(),
        })
        if save:
            self.store.save()
        self.store.event("state", phase=target, code=reason)
        return target

    def restart(self, reason: str = "new_run") -> None:
        current = self.state
        if current == "ready":
            self.transition("prepare", reason)
            return
        if current in TERMINAL_STATES:
            self.transition("prepare", reason)
            return
        raise StateTransitionError(f"当前状态 {current} 不能开始新 Run")
