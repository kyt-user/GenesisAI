"""Select schemas per turn and discover more by capability or exact name."""

import re
import json

FORMAT_TERMS = {
    "xlsx": ("xlsx", "excel", "工作簿", "电子表格"),
    "docx": ("docx", "word", "word文档"),
    "pdf": ("pdf",),
    "pptx": ("pptx", "ppt", "powerpoint", "幻灯片", "演示文稿"),
}

CAPABILITY_TERMS = {
    "fetch_web_content": ("网络", "网页", "联网", "上网", "浏览", "网址", "抓取", "网页阅读", "网络查询"),
    "search_codebase": ("搜代码", "查找代码", "代码搜索", "代码索引", "函数定位"),
    "read_files": ("读取文件", "读文件", "文件内容"),
    "run_commands": ("执行命令", "终端", "运行测试", "跑测试"),
}


def allowed_by_constraints(item, state):
    constraints = state.get("constraints", {})
    return not ((constraints.get("network_forbidden") and item.spec.network)
                or (constraints.get("read_only") and item.spec.side_effect))


def task_routes(state):
    profile = state.get("profile", "direct_answer")
    routes = {"base", profile, *state.get("protocols", []), *state.get("capability_routes", [])}
    if profile.startswith("web") or state.get("reuse_evidence"):
        routes.add("web")
    if state.get("background_jobs"):
        routes.add("jobs")
    if state.get("output_refs"):
        routes.add("tool_output")
    return routes


def select_tools(available, state, skill_tools=()):
    if state.get("history_lookup"):
        return [item for item in available if item.spec.history_lookup]
    routes = task_routes(state)
    protected = set(skill_tools) | {call["name"] for call in state.get("pending_tools", [])}
    if state.get("explicit_discovery"):
        protected.add(state["explicit_discovery"])
    protected.update(item.get("tool") for item in state.get("tool_activity", [])[-6:] if item.get("tool") != "discover_tools")
    protected.update(item.name for item in available if "base" in item.spec.routes)
    words = set(re.findall(r"[a-z][a-z0-9_]+", str(state.get("objective", "")).lower()))
    discovered = list(reversed(state.get("discovered_tools", ())))
    ranks = {name: i for i, name in enumerate(discovered)}
    candidates = [item for item in available if allowed_by_constraints(item, state) and (
        item.name in protected or item.name in ranks or item.name in words or routes.intersection(item.spec.routes))]
    # 基础、活动 Skill、最近使用和待执行工具保留；其余按定义字符预算选择。
    ordered = sorted(candidates, key=lambda item: (item.name not in protected, ranks.get(item.name, len(ranks)), item.name))
    remaining = 24000
    selected = set()
    for item in ordered:
        size = len(json.dumps(item.definition(), ensure_ascii=False))
        if item.name in protected or size <= remaining:
            selected.add(item.name)
            remaining -= size
    return [item for item in available if item.name in selected]


def discover(available, query, limit):
    words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", query.casefold())
    words.extend(name for name, aliases in CAPABILITY_TERMS.items() if any(alias in query for alias in aliases))
    # 中文没有空格时，用短片段补召回；精确工具名仍有最高权重。
    fragments = {word[i:i+2] for word in words if re.fullmatch(r"[\u3400-\u9fff]+", word) for i in range(len(word)-1)}
    words.extend(
        fmt
        for fmt, aliases in FORMAT_TERMS.items()
        if any(alias in query.casefold() for alias in aliases) and fmt not in words
    )
    scored = []
    for item in available:
        fields = " ".join(
            (item.name, item.category, *item.spec.routes, item.description)
        ).casefold()
        score = sum(
            20
            if word == item.name
            else 6
            if word in item.name or word in item.spec.routes
            else 3
            if word == item.category
            else 1
            if word in fields
            else 0
            for word in words
        )
        score += min(3, sum(fragment in fields for fragment in fragments))
        if score:
            scored.append((-score, item.name, item.description))
    return [
        {"name": name, "description": description}
        for _, name, description in sorted(scored)[:limit]
    ]
