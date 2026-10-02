"""把运行状态收敛为继续、回答、恢复或代码验证。"""


class CompletionValidator:
    DECISIONS = frozenset({"continue", "answer", "recover", "verify_code"})

    def decide(self, state: dict) -> str:
        phase = state.get("phase", "explore")
        if phase == "recover":
            return "recover"
        if phase in {"answer", "done"} or state.get("stop_reason"):
            return "answer"
        return "continue"

