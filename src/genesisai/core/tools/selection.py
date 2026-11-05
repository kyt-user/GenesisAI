"""Select schemas per turn and discover more by capability or exact name."""

import re

FORMAT_TERMS = {
    "xlsx": ("xlsx", "excel", "工作簿", "电子表格"),
    "docx": ("docx", "word", "word文档"),
    "pdf": ("pdf",),
    "pptx": ("pptx", "ppt", "powerpoint", "幻灯片", "演示文稿"),
}


def task_routes(state):
    profile = state.get("profile", "direct_answer")
    routes = {"base", profile, *state.get("protocols", [])}
    text = str(state.get("objective", "")).casefold()
    if re.search(r"\b(?:local|files?|directory|workspace)\b", text):
        routes.add("local_files")
    if re.search(r"\b(?:tests?|testing|pytest)\b", text):
        routes.add("testing")
    if re.search(r"\b(?:web|online|internet)\b", text):
        routes.add("web")
    if profile.startswith("web") or state.get("reuse_evidence"):
        routes.add("web")
    if any(
        word in text
        for word in ("图片", "截图", "图像", "image", ".png", ".webp", ".jpg")
    ):
        routes.add("image")
    if any(word in text for word in ("后台", "作业", "job_", "background job")) or state.get("background_jobs"):
        routes.add("jobs")
    if "mcp" in text:
        routes.add("mcp")
    if state.get("output_refs"):
        routes.add("tool_output")
    if any(word in text for word in ("技能", "skill")):
        routes.add("skill")
    if any(
        word in text
        for word in ("历史", "记忆", "之前", "上次", "上一轮", "之前的", "memory")
    ):
        routes.add("memory")
    for fmt, words in FORMAT_TERMS.items():
        if any(word in text for word in words):
            routes.add(fmt)
    return routes


def select_tools(available, state, skill_tools=()):
    if state.get("history_lookup"):
        return [item for item in available if item.spec.history_lookup]
    routes = task_routes(state)
    explicit = set(state.get("discovered_tools", ())) | set(skill_tools)
    words = set(re.findall(r"[a-z][a-z0-9_]+", str(state.get("objective", "")).lower()))
    return [
        item
        for item in available
        if item.name in explicit
        or item.name in words
        or routes.intersection(item.spec.routes)
    ]


def discover(available, query, limit):
    words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", query.casefold())
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
        if score:
            scored.append((-score, item.name, item.description))
    return [
        {"name": name, "description": description}
        for _, name, description in sorted(scored)[:limit]
    ]
