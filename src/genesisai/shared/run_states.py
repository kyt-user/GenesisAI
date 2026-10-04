"""单 Agent Run 的确定性生命周期状态常量。"""

from __future__ import annotations


RUN_STATES = frozenset({
    "ready", "prepare", "plan", "execute", "await_confirmation", "verify",
    "recover", "complete", "partial", "failed", "cancelled", "interrupted",
})
TERMINAL_STATES = frozenset({"complete", "partial", "failed", "cancelled"})
TRANSITIONS = {
    "ready": {"prepare", "cancelled"},
    "prepare": {"plan", "execute", "verify", "failed", "cancelled"},
    "plan": {"execute", "failed", "cancelled"},
    "execute": {"await_confirmation", "verify", "recover", "partial", "failed", "cancelled", "interrupted"},
    "await_confirmation": {"execute", "failed", "cancelled", "interrupted"},
    "verify": {"execute", "recover", "complete", "partial", "failed", "cancelled"},
    "recover": {"execute", "verify", "complete", "partial", "failed", "cancelled", "interrupted"},
    "complete": {"prepare"},
    "partial": {"prepare"},
    "failed": {"prepare"},
    "cancelled": {"prepare"},
    "interrupted": {"execute", "await_confirmation", "failed", "cancelled"},
}