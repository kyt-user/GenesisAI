"""跨层共享的任务档位与初始运行状态契约。"""

from __future__ import annotations

import time

# Profiles select a workflow, never grant or restrict execution budgets.
PROFILES = frozenset({"direct_answer", "web_quick", "web_normal", "web_deep", "local_files", "creative"})


def is_recall_request(text: str) -> bool:
    value = text.casefold()
    return any(word in value for word in ("复述", "再说一下", "刚才查到的", "重复刚才"))


def initial_run_runtime(profile: str = "direct_answer", *, phase: str = "explore") -> dict:
    """构造一个新 Run 的确定性运行状态字典。"""
    if profile not in PROFILES:
        raise ValueError("Unknown task profile: " + profile)
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
        "budget": {"model_calls": None, "tools": None, "total_tokens": None},
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
