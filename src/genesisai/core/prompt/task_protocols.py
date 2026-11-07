"""任务协议选择：根据用户输入与活动任务推断协议集合与继续意图。

从 `agent/loop.py` 拆出（规格 §6），属 L4 core；agent 经 `PromptPort` 调用，
自身不 import 任何 core.*。
"""

from __future__ import annotations

import re

from genesisai.shared.budgets import is_recall_request
from genesisai.core.tools.selection import FORMAT_TERMS


_URL = re.compile(r"https?://[^\s<>()]+", re.I)
_NETWORK_DENIAL = re.compile(r"禁止联网|不要联网|不许联网|离线(?:处理|完成|操作)|do not (?:access the network|browse)|no network", re.I)
_DISCUSSION = re.compile(r"先讨论|先规划|只讨论|只读|仅分析|不要修改|不要写|暂时不要|不要执行|do not (?:modify|edit|execute)|read.only", re.I)
_LOCAL_ACTION = re.compile(r"修复|重构|实现|编写|修改|运行|跑测试|构建|开发|移动文件|删除文件|(?<![a-z])(?:fix|refactor|implement|pytest)(?![a-z])", re.I)
_BUILDER_REQUEST = re.compile(
    r'创造模式|^/?create_(?:tool|skill|mcp)\b|能力库'
    r'|(?:创建|构建|制作|开发|注册|修订|测试|试用).{0,16}(?:外挂|扩展能力|skill|mcp)'
    r'|(?:创建|构建|制作|开发).{0,8}(?:可复用|可重复使用).{0,6}(?:工具|tool\b)'
    r'|\b(?:create|build|develop)\b.{0,24}\breusable\s+(?:tool|skill)\b'
    r'|\b(?:create|build|register|revise|test)\b.{0,24}\b(?:mcp|skill)\b', re.I)


def asks_for_explanation(text):
    return bool(re.match(r'^\s*(?:解释|说明|讨论|如何|怎么|为什么|是否支持|能否支持|explain\b|how\b|why\b)', text, re.I))


def new_task_requested(text):
    # Mentioning or negating "新任务" is not a command to discard the active goal.
    return bool(re.search(r'(?:^|[。！？!?\n])\s*(?:(?:好|好的)[，,]\s*)?'
                          r'(?:(?:现在|接下来)(?:开始|是)?|开始|这是(?:一个)?)?'
                          r'(?:新任务|另一个任务|换个话题|new task)(?:[\s:：，,。.!！?？]|$)', text, re.I))


def select_initial_profile(text: str) -> str:
    """只选择起始工作流程；能力可组合，权限不由分类结果授予。"""
    value = _URL.sub(" ", text.casefold())
    if is_recall_request(value):
        return "direct_answer"
    if not _NETWORK_DENIAL.search(value):
        if any(word in value for word in ("深度调研", "深入调研", "全面调研", "deep research")):
            return "web_deep"
        if any(word in value for word in ("专题报告", "比较并整理", "对比报告", "研究报告")):
            return "web_normal"
    local_action = bool(_LOCAL_ACTION.search(value))
    local_path = bool(re.search(r"[a-z]:[\\/]|(?:^|\s)(?:\./|/[^/])", value))
    if not _NETWORK_DENIAL.search(value) and not local_action and not local_path and (
        _URL.search(text) or re.search(r"(?<![a-z])(?:github|gitlab)(?![a-z])", value)
        or any(word in value for word in ("官网", "联网", "上网", "网上查", "网络查询", "搜索", "检索", "目前", "当前价格", "今天", "近期", "最新", "售价", "多少钱"))
    ):
        return "web_quick"
    if any(word in value for word in ("写小说", "续写", "扩写", "角色", "章节", "故事", "剧情", "世界观", "人物设定", "情节", "写作", "创作", "第一章", "下一章")):
        return "creative"
    if local_action or local_path or any(word in value for word in (
        "本地文件", "本地资料", "这个目录", "工作区", "代码", "仓库", "测试", "报错", "文件管理", "办公文档", "工作簿", "演示文稿",
    )) or re.search(r"(?<![a-z0-9_])(?:git|bug|python|pyproject|pip|ruff|mypy|skill|docx|xlsx|pptx|pdf)(?![a-z0-9_])", value):
        return "local_files"
    return "direct_answer"

_CONTINUATION_EXACT = {
    "开始",
    "开始吧",
    "继续",
    "继续吧",
    "继续实现",
    "继续执行",
    "开始实现",
    "开始执行",
    "开始写",
    "动手吧",
    "就这样",
    "按这个来",
    "按默认来",
    "按你的默认来",
    "就按默认来",
    "就按你的默认来",
    "照默认来",
    "可以",
    "可以开始",
    "同意",
    "确认",
    "没问题",
    "照这个方案做",
    "按这个方案做",
}
_DEFAULT_PLAN_EXACT = {
    "就这样",
    "按这个来",
    "按默认来",
    "按你的默认来",
    "就按默认来",
    "就按你的默认来",
    "照默认来",
    "照这个方案做",
    "按这个方案做",
    "可以",
    "同意",
    "确认",
}


def _normalize(text: str) -> str:
    """去除空白与常见中英文标点，并转小写。"""
    return re.sub(r"[\s，。！？!,.；;：:]+", "", (text or "").casefold())


class TaskProtocolSelector:
    """按关键词判定继续意图与所需协议集合（原 Runner 静态谓词，属 L4 core）。"""

    def is_development_continuation(self, text: str, active_task) -> bool:
        """判断当前输入是否明确要求继续已有开发任务。"""
        if not active_task or active_task.get("status") in {"completed", "cancelled", "superseded"}:
            return False
        if 'capability_builder' in active_task['execution_context'].get('protocols', []):
            return False
        if 'coding' not in active_task['execution_context'].get('protocols', []) and not active_task['execution_requested']:
            return False
        value = _normalize(text)
        if _DISCUSSION.search(text) or new_task_requested(text) or re.search(r"停止|取消", text):
            return False
        return value in _CONTINUATION_EXACT or bool(re.match(
            r"(?:请|帮我)?(?:继续(?:实现|执行|修复|修改|开发|完成)|按(?:刚才|上述|之前|这个|该)(?:的)?(?:计划|方案|清单).*(?:实现|执行|修复|完成))", value
        ))

    def accepts_default_plan(self, text: str) -> bool:
        """判断文本是否表示接受默认计划。"""
        return _normalize(text) in _DEFAULT_PLAN_EXACT

    def requests_development_execution(self, text: str, protocols) -> bool:
        """判断用户输入是否明确要求执行开发操作。"""
        if "coding" not in protocols:
            return False
        value = text.casefold()
        if _DISCUSSION.search(value) or "有什么要问" in value:
            return False
        if asks_for_explanation(text):
            return False
        return any(
            word in value
            for word in ("实现", "修复", "编写", "创建", "构建", "开发", "修改")
        )

    def __init__(self):
        self.selectors = {}

    def route(self, text: str, active_task=None, previous=None) -> dict:
        new_task = new_task_requested(text)
        continuation = self.is_development_continuation(text, active_task)
        followup = continuation or bool(re.search(r"刚才|上述|上面|之前|你不是|再试|重试|为什么|怎么会|继续", text))
        builder_continuation = bool(previous and "capability_builder" in previous.get("capability_routes", [])
                                    and (followup or _normalize(text) in _CONTINUATION_EXACT
                                         or re.search(r"开始(?:创建|构建|制作)|^(?:那就|就按|按你|不用|不保|保留|不要保)", text)))
        if new_task:
            followup = continuation
            builder_continuation = False
        followup |= builder_continuation
        profile = "local_files" if continuation else select_initial_profile(text)
        protocols = self.protocols_for(text)
        if builder_continuation:
            protocols = ["capability_builder"]
        if "capability_builder" in protocols:
            profile = "local_files"
        if continuation and "coding" not in protocols:
            protocols.append("coding")
        routes = {profile, *protocols}
        value = _URL.sub(" ", text.casefold())
        for route, pattern in {
            "capability_builder": _BUILDER_REQUEST,
            "local_files": r"\b(?:local|files?|directory|workspace)\b",
            "testing": r"\b(?:tests?|testing|pytest)\b",
            "web": r"\b(?:web|online|internet)\b",
            "image": r"图片|截图|图像|image|\.png|\.webp|\.jpg",
            "jobs": r"后台|作业|job_|background job",
            "mcp": r"mcp", "skill": r"技能|skill",
            "memory": r"历史|记忆|之前|上次|上一轮|memory",
        }.items():
            if re.search(pattern, value):
                routes.add(route)
        for fmt, words in FORMAT_TERMS.items():
            if any(word in value for word in words):
                routes.add(fmt)
        if not _NETWORK_DENIAL.search(text) and (profile.startswith("web") or _URL.search(text) or any(w in text for w in ("联网", "上网", "官网", "查文档"))):
            routes.add("web")
        constraints = {"network_forbidden": bool(_NETWORK_DENIAL.search(text)), "read_only": bool(_DISCUSSION.search(text))}
        constraint_updates = [{'key': key, 'value': True, 'scope': 'task'} for key, value in constraints.items() if value]
        if followup and previous:
            routes.update(previous.get("capability_routes", []))
            for key in constraints:
                constraints[key] |= previous.get("constraints", {}).get(key, False)
            if re.search(r"(?:现在)?(?:允许联网|可以联网)|联网权限已恢复", text):
                constraints["network_forbidden"] = False
                constraint_updates.append({'key': 'network_forbidden', 'value': False, 'scope': 'task'})
            if self.requests_development_execution(text, protocols) and not _DISCUSSION.search(text):
                constraints["read_only"] = False
                constraint_updates.append({'key': 'read_only', 'value': False, 'scope': 'task'})
            if builder_continuation and not _DISCUSSION.search(text) and re.search(r"开始(?:创建|构建|制作|执行)", text):
                constraints["read_only"] = False
                constraint_updates.append({'key': 'read_only', 'value': False, 'scope': 'task'})
        if re.search(r'(?:允许联网|可以联网)|联网权限已恢复', text) and not _NETWORK_DENIAL.search(text):
            constraints['network_forbidden'] = False
            constraint_updates.append({'key': 'network_forbidden', 'value': False, 'scope': 'task'})
        if (self.requests_development_execution(text, protocols)
                or _normalize(text) in {'开始执行', '继续执行', '开始写', '动手吧', '照这个方案做', '按这个方案做'}) and not _DISCUSSION.search(text):
            constraints['read_only'] = False
            constraint_updates.append({'key': 'read_only', 'value': False, 'scope': 'task'})
        constraint_updates = list({item['key']: item for item in constraint_updates}.values())
        builder_execution = "capability_builder" in protocols and not constraints["read_only"] and not asks_for_explanation(text)
        capability_goal = (previous or {}).get("capability_goal", "registered") if builder_continuation else "registered"
        if builder_execution:
            if re.search(r"(?:只|仅|先).{0,4}(?:创建|构建)?草稿|不要注册|暂不注册", text):
                capability_goal = "draft"
            elif re.search(r"(?:只|仅|先).{0,4}(?:测试|试用)", text):
                capability_goal = "tested"
            elif re.search(r"注册|安装|连接上|接入", text):
                capability_goal = "registered"
        return {
            "new_task": new_task,
            "constraint_updates": constraint_updates,
            "capability_goal": capability_goal,
            "profile": profile,
            "protocols": protocols,
            "capability_routes": sorted(routes),
            "constraints": constraints,
            "followup": followup,
            "development_continuation": continuation and not constraints["read_only"],
            "execution_requested": not constraints["read_only"] and (builder_execution or continuation or self.requests_development_execution(text, protocols)),
        }

    def register(self, name, selector):
        if not name or name in self.selectors or not callable(selector):
            raise ValueError("Protocol selector requires a unique name and callable")
        self.selectors[name] = selector

    def protocols_for(self, text: str) -> list:
        """根据用户输入关键词推断所需的协议列表。"""
        value = _URL.sub(" ", text.casefold())
        result = []
        if _BUILDER_REQUEST.search(value):
            return ["capability_builder"]
        if any(
            word in value
            for word in (
                "代码",
                "仓库",
                "修复",
                "bug",
                "重构",
                "实现",
                "报错",
                "编写",
                "开发",
                "函数",
                "python",
                "pyproject",
                "pip",
                "ruff",
                "mypy",
            )
        ):
            result.append("coding")
        if any(
            word in value
            for word in (
                "测试",
                "pytest",
                "unittest",
                "npm test",
                "cargo test",
                "跑测试",
                "跑一下测试",
            )
        ):
            result.append("testing")
        if re.search(r"(?<![a-z])(?:git|diff)(?![a-z])|提交记录|分支", value):
            result.append("git_read")
        if any(
            word in value for word in ("文件", "目录", "移动", "重命名", "删除", "整理")
        ):
            result.append("file_management")
        if any(
            word in value
            for word in (
                "docx",
                "xlsx",
                "pptx",
                "pdf",
                "word",
                "excel",
                "powerpoint",
                "办公文档",
            )
        ):
            result.append("office")
        if any(
            word in value
            for word in (
                "调试",
                "debug",
                "排查",
                "为什么失败",
                "异常",
                "traceback",
                "错误信息",
            )
        ):
            result.append("debugging")
        if any(
            word in value
            for word in (
                "写小说",
                "续写",
                "扩写",
                "章节",
                "故事",
                "剧情",
                "角色",
                "世界观",
                "创作",
                "写作",
            )
        ):
            result.append("creative_writing")
        if any(word in value for word in ("修改", "润色", "改写", "修订", "审稿")):
            result.append("revision")
        result.extend(
            name
            for name, selector in self.selectors.items()
            if selector(text) and name not in result
        )
        return result
