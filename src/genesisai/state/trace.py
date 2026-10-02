"""只写入脱敏执行元数据的 Trace 辅助器。"""

from __future__ import annotations

from genesisai.state.store import uid


class TraceRecorder:
    def __init__(self, store):
        self.store = store

    def model_start(self, state: dict) -> str:
        call_id = uid("model")
        self.store.event(
            "model_start",
            model_call_id=call_id,
            profile=state.get("profile"),
            phase=state.get("phase"),
            prompt_names=state.get("prompt_names", []),
            prompt_hashes=state.get("prompt_hashes", {}),
            context_report=state.get("context_report", {}),
        )
        return call_id

    def model_end(self, call_id: str, *, elapsed_ms: int, finish_reason, usage: dict, state: dict) -> None:
        self.store.event(
            "model_end",
            model_call_id=call_id,
            elapsed_ms=elapsed_ms,
            finish_reason=finish_reason,
            usage=usage,
            profile=state.get("profile"),
            phase=state.get("phase"),
            stop_reason=state.get("stop_reason"),
            evidence_count=len(state.get("evidence_refs", [])),
        )

