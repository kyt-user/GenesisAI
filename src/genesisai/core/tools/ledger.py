"""持久化工具调用状态并写入脱敏审计事件。"""

from genesisai.shared.security import ToolError


class CallLedger:
    """持久化工具调用状态并写入脱敏审计事件。"""
    def __init__(self, store):
        self.store = store

    def prior(self, call: dict) -> dict | None:
        prior = self.store.data["calls"].get(call["id"])
        if prior and prior["call"] != call:
            raise ToolError("duplicate_call_id", "调用 ID 已用于其他参数")
        return prior

    def prepare(self, call: dict, preview: dict, digest_value: str, expires: float) -> None:
        self.store.data["calls"][call["id"]] = {
            "call": call,
            "state": "prepared",
            "preview": preview,
            "digest": digest_value,
            "expires": expires,
        }
        self.store.save()

    def start(self, call: dict) -> None:
        self.store.data["calls"][call["id"]] = {"call": call, "state": "started"}
        self.store.save()

    def finish(self, call: dict, result: dict, elapsed_ms: int) -> None:
        state = "succeeded" if result["ok"] else "failed"
        executed = self.store.data['calls'].get(call['id'], {}).get('executed', False)
        self.store.data["calls"][call["id"]] = {"call": call, "state": state, "result": result, "executed": executed}
        self.store.save()
        self.store.event(
            "tool",
            call_id=call["id"],
            tool=call["name"],
            ok=result["ok"],
            code=result["error"]["code"] if result["error"] else None,
            elapsed_ms=elapsed_ms,
        )

    def unknown(self, call: dict, result: dict) -> None:
        self.store.data["calls"][call["id"]] = {
            "call": call,
            "state": "unknown",
            "result": result,
        }
        self.store.save()

    def reject(self, call: dict, result: dict) -> None:
        self.store.data["calls"][call["id"]] = {
            "call": call,
            "state": "failed",
            "result": result,
        }
        self.store.save()

