"""Bound the model-facing text while retaining the complete result for exact reads."""

import time
from hashlib import sha256

from genesisai.core.persistence.database import encode
from genesisai.shared.messages import image_parts
from genesisai.shared.results import result_content


def project_output(store, call, result, limit):
    # Completion control is host metadata. The model gets the tool's payload.
    visible = {k: v for k, v in result.items() if k != "completion_candidate"}
    content = result_content(visible)
    text = content if isinstance(content, str) else content[0]["text"]
    if len(text) <= limit:
        return content
    identity = (
        "output_" + sha256(encode([store.id, call["id"], text]).encode()).hexdigest()
    )
    with store.database.transaction() as db:
        db.execute(
            "INSERT OR IGNORE INTO events(id,session_id,seq,run_id,kind,payload,created) "
            "SELECT ?,?,coalesce(max(seq),0)+1,?,'tool_output',?,? FROM events WHERE session_id=?",
            (identity, store.id, store.record.run_id, text, time.time(), store.id),
        )
    store.record.run_runtime["output_refs"] = True
    envelope = {
        "ok": visible["ok"],
        "truncated": True,
        "ref": identity,
        "chars": len(text),
    }
    if not visible["ok"]:
        envelope["code"] = str((visible.get("error") or {}).get("code", "tool_error"))[
            :32
        ]
        if len(encode({**envelope, "preview": ""})) > limit:
            del envelope["code"]
    # JSON escaping varies. Find a prefix that fits the actual serialized cap.
    lo, hi = 0, min(limit, len(text))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(encode({**envelope, "preview": text[:mid]})) <= limit:
            lo = mid
        else:
            hi = mid - 1
    bounded = encode({**envelope, "preview": text[:lo]})
    images = image_parts(content)
    return [{"type": "text", "text": bounded}, *images] if images else bounded
