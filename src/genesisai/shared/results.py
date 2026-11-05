"""跨层共享的工具结果封套。"""

from __future__ import annotations
import json

from genesisai.shared.messages import validate_content


def result_content(result: dict):
    """Tool envelope remains JSON text; image blocks carry validated asset refs."""
    images = (result.get('data') or {}).get('images', []) if result.get('ok') else []
    text = json.dumps(result, ensure_ascii=False)
    if not images:
        return text
    validate_content(images)
    if any(part['type'] != 'image' for part in images):
        raise ValueError('工具 images 字段只能包含图片引用')
    return [{'type': 'text', 'text': text}, *images]


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
