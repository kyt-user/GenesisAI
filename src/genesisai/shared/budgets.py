"""跨层共享的任务档位与初始运行状态契约。"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class TaskProfile:
    name: str
    model_calls: int
    search_queries: int
    search_fetches: int
    retries_per_candidate: int

    def to_dict(self) -> dict:
        return asdict(self)


PROFILES = {
    "direct_answer": TaskProfile("direct_answer", 3, 0, 0, 0),
    "web_quick": TaskProfile("web_quick", 8, 2, 4, 1),
    "web_normal": TaskProfile("web_normal", 12, 4, 8, 1),
    "web_deep": TaskProfile("web_deep", 20, 8, 12, 1),
    "local_files": TaskProfile("local_files", 20, 4, 8, 1),
    "creative": TaskProfile("creative", 25, 2, 4, 1),
}


def is_recall_request(text: str) -> bool:
    value = text.casefold()
    return any(word in value for word in ("复述", "再说一下", "刚才查到的", "重复刚才"))


def initial_run_runtime(profile: str = "direct_answer", *, phase: str = "explore") -> dict:
    """构造一个新 Run 的确定性运行状态字典。"""
    selected = PROFILES[profile]
    return {
        "profile": profile,
        "phase": phase,
        "lifecycle": "ready" if phase == "explore" else ("complete" if phase == "done" else "recover"),
        "state_history": [],
        "protocols": [],
        "capability_routes": [],
        "constraints": {},
        "capability_blocks": {},
        "context_summary": None,
        "context_report": {},
        "budget": selected.to_dict(),
        "usage": {
            "model_calls": 0,
            "search_queries": 0, "search_fetches": 0,
            "observations": 0, "total_tokens": 0,
        },
        "candidates": {},
        "candidate_count": 0,
        "fetched_count": 0,
        "content_hashes": [],
        "evidence": {},
        "evidence_refs": [],
        "failed_resources": {},
        "recovery_counts": {"length": 0, "empty": 0, "tool_protocol": 0, "provider": 0},
        "counted_calls": [],
        "completed_queries": [],
        "progress_mark": [0, 0],
        "stalled_rounds": 0,
        "stop_reason": None,
        "partial_response": "",
        "last_tool_name": None,
        "last_query_relevant": False,
        "reuse_evidence": False,
        "allow_new_network": False,
        "confirmation_paused_at": None,
        "paused_seconds": 0,
        "last_failure": None,
        "observed_spans": [],
        "tool_activity": [],
        "search_diagnostics": {},
        "prompt_names": [],
        "prompt_hashes": {},
        "verification_rounds": 0,
        "verification_required": False,
        "started_at": time.time(),
    }
