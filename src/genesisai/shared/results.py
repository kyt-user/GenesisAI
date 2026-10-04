"""跨层共享的工具结果封套。"""

from __future__ import annotations


def error_result(call: dict, code: str, message: str, retryable: bool = False) -> dict:
    return {
        "call_id": call.get("id", "") if isinstance(call, dict) else "",
        "ok": False,
        "data": None,
        "error": {"code": code, "message": message, "retryable": retryable},
        "source_refs": [],
        "artifact_refs": [],
        "truncated": False,
    }