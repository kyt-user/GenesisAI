"""持久化工具调用状态并写入脱敏审计事件。"""

from genesisai.shared.security import ToolError


class CallLedger:
    """持久化工具调用状态并写入脱敏审计事件。"""
    def __init__(self, store):
        self.store = store

    def prior(self, call: dict) -> dict | None:
        prior = self.store.record.calls.get(call['id'])
        if prior and prior["call"] != call:
            raise ToolError("duplicate_call_id", "调用 ID 已用于其他参数")
        return prior

    def prepare(self, call: dict, preview: dict, digest_value: str) -> None:
        self.store.record.calls[call['id']] = {
            "call": call,
            "state": "prepared",
            "preview": preview,
            "digest": digest_value,
        }
        self.store.commit()

    def await_input(self, call, question):
        self.store.record.calls[call["id"]] = {"call": call, "state": "awaiting_input",
                                               "question": question, "executed": False}
        self.store.commit()

    def start(self, call: dict) -> None:
        self.store.record.calls[call['id']] = {"call": call, "state": "started", "executed": True}
        self.store.commit()

    def finish(self, call: dict, result: dict, elapsed_ms: int) -> None:
        state = "succeeded" if result["ok"] else "failed"
        executed = self.store.record.calls.get(call['id'], {}).get('executed', False)
        self.store.record.calls[call['id']] = {"call": call, "state": state, "result": result, "executed": executed}
        self.store.commit()
        self.store.event(
            "tool",
            call_id=call["id"],
            tool=call["name"],
            ok=result["ok"],
            code=result["error"]["code"] if result["error"] else None,
            elapsed_ms=elapsed_ms,
        )

    def unknown(self, call: dict, result: dict) -> None:
        executed = self.store.record.calls.get(call['id'], {}).get('executed')
        self.store.record.calls[call['id']] = {
            "call": call,
            "state": "unknown",
            "result": result,
            "executed": executed,
        }
        self.store.commit()

    def reject(self, call: dict, result: dict) -> None:
        self.store.record.calls[call['id']] = {
            "call": call,
            "state": "failed",
            "result": result,
        }
        self.store.commit()

