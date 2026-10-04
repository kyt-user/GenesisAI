"""跨层共享的任务档位与初始运行状态契约。"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class TaskProfile:
    name: str
    model_calls: int
    search_queries: int
    search_fetches: int
    retries_per_candidate: int
    token_budget: int
    seconds: int

    def to_dict(self) -> dict:
        return asdict(self)


PROFILES = {
    "direct_answer": TaskProfile("direct_answer", 3, 0, 0, 0, 12000, 60),
    "web_quick": TaskProfile("web_quick", 8, 2, 4, 1, 30000, 120),
    "web_normal": TaskProfile("web_normal", 12, 4, 8, 1, 50000, 300),
    "web_deep": TaskProfile("web_deep", 20, 8, 12, 1, 100000, 600),
    "local_files": TaskProfile("local_files", 20, 0, 0, 1, 160000, 600),
    "creative": TaskProfile("creative", 25, 0, 0, 1, 120000, 600),
}


def is_recall_request(text: str) -> bool:
    value = text.casefold()
    return any(word in value for word in ("复述", "再说一下", "刚才查到的", "重复刚才"))


def select_initial_profile(text: str) -> str:
    """只识别明确高成本或本地任务；普通请求仍从 direct_answer 开始。"""
    value = text.casefold()
    if any(word in value for word in ("深度调研", "深入调研", "全面调研", "deep research")):
        return "web_deep"
    if any(word in value for word in ("专题报告", "比较并整理", "对比报告", "研究报告")):
        return "web_normal"
    if any(word in value for word in (
        "本地文件", "本地资料", "这个目录", "工作区文件", "代码", "仓库", "测试",
        "修复", "bug", "报错", "重构", "git", "文件管理", "移动文件", "删除文件",
        "pytest", "python", "pyproject", "pip", "ruff", "mypy", "skill",
        "docx", "xlsx", "pptx", "pdf", "办公文档", "工作簿", "演示文稿",
    )):
        return "local_files"
    if any(word in value for word in (
        "写小说", "续写", "扩写", "角色", "章节", "故事", "剧情", "世界观",
        "人物设定", "情节", "写作", "创作", "第一章", "下一章",
    )):
        return "creative"
    if is_recall_request(text):
        return "direct_answer"
    if re.search(r"https?://", value) or any(word in value for word in (
        "官网", "联网", "网上查", "网络查询", "搜索", "检索", "目前", "当前价格",
        "今天", "近期", "最新", "售价", "多少钱",
    )):
        return "web_quick"
    return "direct_answer"


def initial_run_runtime(profile: str = "direct_answer", *, phase: str = "explore") -> dict:
    """构造一个新 Run 的确定性运行状态字典。"""
    selected = PROFILES[profile]
    return {
        "profile": profile,
        "phase": phase,
        "lifecycle": "ready" if phase == "explore" else ("complete" if phase == "done" else "recover"),
        "state_history": [],
        "protocols": [],
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