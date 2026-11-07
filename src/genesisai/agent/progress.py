"""Detect repeated unproductive actions without imposing a task lifetime.

Only new observed results advance the epoch. Model-written progress claims do
not reset recovery. The state is part of the durable run checkpoint.
"""

import hashlib
import json


_VOLATILE = frozenset({
    "call_id", "source_ref", "source_refs", "artifact_refs", "fetched_at",
    "timestamp", "elapsed_ms", "duration_ms", "started_at", "updated_at",
})


def fingerprint(value):
    def stable(item):
        if isinstance(item, dict):
            return {k: stable(v) for k, v in item.items() if k not in _VOLATILE}
        if isinstance(item, (list, tuple)):
            return [stable(v) for v in item]
        return item

    payload = json.dumps(stable(value), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ProgressTracker:
    def __init__(self, state, *, repeated_action_limit=4):
        if type(repeated_action_limit) is not int or repeated_action_limit < 2:
            raise ValueError("repeated_action_limit must be an integer >= 2")
        self.data = state.setdefault("progress", {"epoch": 0, "observed": {}, "attempts": {}})
        self.limit = repeated_action_limit

    def retry(self, kind, identity):
        """Return False only for this repeated action without intervening progress."""
        key = fingerprint([kind, identity])
        count = self.data["attempts"].get(key, 0) + 1
        self.data["attempts"][key] = count
        return count < self.limit

    def observe(self, call, result):
        """Record actual results; background polling is waiting, not a retry."""
        data = result.get("data") or {}
        if call["name"] == "read_job" and data.get("status") in {"queued", "running", "stopping"}:
            return True
        try:
            arguments = json.loads(call.get("arguments", "{}"))
        except (TypeError, ValueError):
            arguments = call.get("arguments")
        identity = [call["name"], arguments, data, result.get("error")]
        if result.get("ok"):
            signature = fingerprint(identity)
            if signature not in self.data["observed"]:
                self.data["observed"][signature] = self.data["epoch"] + 1
                self.data["epoch"] += 1
                self.data["attempts"].clear()
                return True
        return self.retry("tool_result", identity)
