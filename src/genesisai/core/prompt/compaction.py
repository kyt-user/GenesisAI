"""对齐 cline 的上下文压缩模型（basic / agentic / off）。

参考 cline `extensions/context/` 下的 `compaction.ts`、`basic-compaction.ts`
与 `compaction-shared.ts`：

- 触发比例 ``COMPACTION_TRIGGER_RATIO = 0.9``：转录消耗到可用输入预算的该
  比例时触发压缩。
- 目标比例 ``DEFAULT_TARGET_RATIO = 0.7``：压缩后回落到触发线的该比例。
- 保留近端 ``DEFAULT_PRESERVE_RECENT_TOKENS = 20000``：最近内容按 token 保留。
- ``CHARS_PER_TOKEN = 3``：字符到 token 的确定性估算。

``basic`` 为确定性折叠：逐字保留所有 typed user 提示，最新轮次在目标内保留其
最新消息（切点对齐到 assistant 边界，避免拆散工具调用/结果对），旧轮次保留其
结论性 assistant 回答；被丢弃区间的工具活动重新呈现为 dropped-work 提示块，
附加到前一个存活的 user 提示上。``agentic`` 依赖摘要模型，本实现缺少可用摘要器
时回退 ``basic``（与 cline 的降级一致）；``off`` 关闭压缩。
"""

from __future__ import annotations

import json


# --- cline 常量（compaction-shared.ts） -------------------------------------
CHARS_PER_TOKEN = 3
DEFAULT_MAX_INPUT_TOKENS = 128_000
CONTEXT_WINDOW_INPUT_RATIO = 0.9
COMPACTION_TRIGGER_RATIO = 0.9
DEFAULT_TARGET_RATIO = 0.7
DEFAULT_PRESERVE_RECENT_TOKENS = 20_000
TOOL_RESULT_CHAR_LIMIT = 2_000
COMMAND_SUMMARY_CHAR_LIMIT = 100
# basic 折叠时逐字保留的最近助手回答条数。
PRESERVED_ASSISTANT_TEXT_COUNT = 3

COMPACTION_STRATEGIES = ("basic", "agentic", "off")

_SYSTEM_NOTICE_OPEN = "<SYSTEM_NOTICE>\nEarlier context was compacted. "


def estimate_tokens(char_count: int) -> int:
    """按 cline 的 CHARS_PER_TOKEN 估算 token 数（向上取整，至少为 1）。"""
    if char_count <= 0:
        return 1
    return -(-char_count // CHARS_PER_TOKEN)


def estimate_message_tokens(message: dict) -> int:
    """以序列化长度估算单条消息的 token 数（cline `createTokenEstimator`）。"""
    try:
        serialized = json.dumps(message, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        serialized = str(message)
    return estimate_tokens(len(serialized))


def total_tokens(messages: list[dict]) -> int:
    """按消息估算之和返回转录 token 数。"""
    return sum(estimate_message_tokens(message) for message in messages)


def trigger_and_target_tokens(
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    *,
    trigger_ratio: float = COMPACTION_TRIGGER_RATIO,
    target_ratio: float = DEFAULT_TARGET_RATIO,
) -> tuple[int, int]:
    """返回 (触发线, 目标线) 的 token 预算。"""
    trigger = max(1, int(max_input_tokens * trigger_ratio))
    target = max(1, int(trigger * target_ratio))
    return trigger, target


def resolve_strategy(strategy: str | None) -> str:
    """规范化策略名；未知值回退 ``basic``，与 cline 的默认一致。"""
    value = (strategy or "basic").strip().lower()
    return value if value in COMPACTION_STRATEGIES else "basic"


def is_turn_start(message: dict) -> bool:
    """typed user 轮次起点（GenesisAI 中工具结果是独立的 role=tool 消息）。"""
    return message.get("role") == "user"


def _is_concluding_answer(message: dict) -> bool:
    """旧轮次的结论性回答：无工具调用的文本 assistant 消息。"""
    return (
        message.get("role") == "assistant"
        and not message.get("tool_calls")
        and bool(str(message.get("content") or "").strip())
    )


# --- dropped-work 摘要（compaction-shared.ts summarizeToolActivity） ---------

def _call_args(call: dict) -> dict:
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return arguments if isinstance(arguments, dict) else {}


def _push_unique(target: list[str], value: str) -> None:
    text = str(value or "").strip()
    if text and text not in target:
        target.append(text)


def _collect_paths(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        paths: list[str] = []
        for item in value:
            paths.extend(_collect_paths(item))
        return paths
    if isinstance(value, dict):
        paths = []
        for key in ("path", "file_path", "source", "destination"):
            paths.extend(_collect_paths(value.get(key)))
        return paths
    return []


def _truncate_command(command: str) -> str:
    text = " ".join(str(command).split())
    if len(text) <= COMMAND_SUMMARY_CHAR_LIMIT:
        return text
    return text[:COMMAND_SUMMARY_CHAR_LIMIT] + "..."


def summarize_tool_activity(messages: list[dict]) -> dict:
    """折叠被丢弃区间内的工具活动：读取/编辑的文件与运行的命令。"""
    read_files: list[str] = []
    edited_files: list[str] = []
    commands: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            name = call.get("name")
            args = _call_args(call)
            if name == "read_files":
                targets = args.get("files") or args.get("path")
                for path in _collect_paths(targets):
                    _push_unique(read_files, path)
            elif name in ("editor", "apply_patch"):
                for path in _collect_paths(args):
                    _push_unique(edited_files, path)
            elif name == "run_commands":
                command = args.get("command")
                if isinstance(command, list):
                    _push_unique(commands, _truncate_command(" ".join(str(item) for item in command)))
                elif isinstance(command, str):
                    _push_unique(commands, _truncate_command(command))
    return {"read_files": read_files, "edited_files": edited_files, "commands": commands}


def has_tool_activity(activity: dict) -> bool:
    return bool(activity.get("read_files") or activity.get("edited_files") or activity.get("commands"))


def render_dropped_work(activity: dict, preserved_responses: list[str] | None = None) -> str:
    """渲染 cline 风格的 dropped-work 通知块文本。"""
    sections = [
        "Files read:\n" + ("\n".join(activity.get("read_files") or []) or "none"),
        "Files edited:\n" + ("\n".join(activity.get("edited_files") or []) or "none"),
        "Commands ran:\n" + ("\n".join(activity.get("commands") or []) or "none"),
    ]
    body = "\n\n".join(sections)
    responses = [text for text in (preserved_responses or []) if text]
    if responses:
        body += "\n\nYour recent responses:\n" + "\n---\n".join(responses)
    return _SYSTEM_NOTICE_OPEN + "Summary of your actions after the request above:\n" + body + "\n</SYSTEM_NOTICE>"


# --- basic 折叠（basic-compaction.ts runBasicCompaction） --------------------

def turn_bounds(messages: list[dict]) -> list[tuple[int, int]]:
    """按 typed user 起点把消息切成连续的轮次区间 ``[start, end)``。"""
    starts = [index for index, message in enumerate(messages) if is_turn_start(message)]
    if not starts:
        return []
    bounds = starts + [len(messages)]
    return [(bounds[index], bounds[index + 1]) for index in range(len(starts))]


def _preserved_responses(span: list[dict]) -> list[str]:
    """被丢弃区间内最近的若干条 assistant 文本回答（cline 的逐字保留窗口）。"""
    contents = [
        str(message.get("content"))
        for message in span
        if message.get("role") == "assistant" and str(message.get("content") or "").strip()
    ]
    return contents[-PRESERVED_ASSISTANT_TEXT_COUNT:]


def _attach_notice(folded: list[dict], notice: str) -> None:
    """把 dropped-work 通知附加到前一个存活的 typed user 提示上。"""
    for index in range(len(folded) - 1, -1, -1):
        if is_turn_start(folded[index]):
            message = dict(folded[index])
            message["content"] = (str(message.get("content") or "") + "\n\n" + notice).strip()
            folded[index] = message
            return


def fold_messages(
    messages: list[dict],
    *,
    recent_turns: int = 2,
    preserve_recent_tokens: int = DEFAULT_PRESERVE_RECENT_TOKENS,
) -> tuple[list[dict], dict]:
    """cline 风格折叠，返回 ``(折叠后的消息, 报告)``。

    - 全部 typed user 提示逐字保留；
    - 最近 ``recent_turns`` 个轮次逐字保留；
    - 旧轮次仅保留其结论性 assistant 回答；
    - 被丢弃区间的工具活动折叠为 dropped-work 提示块。
    """
    count = len(messages)
    report = {
        "folded": False,
        "messages_before": count,
        "messages_after": count,
        "messages_removed": 0,
        "dropped_work": {"read_files": [], "edited_files": [], "commands": []},
        "dropped_work_notice": None,
    }
    if count < 2:
        return [dict(message) for message in messages], report
    turns = turn_bounds(messages)
    if len(turns) <= recent_turns:
        return [dict(message) for message in messages], report

    keep: set[int] = set()
    for start, _ in turns:
        keep.add(start)
    recent_from = len(turns) - recent_turns
    for index in range(recent_from, len(turns)):
        start, end = turns[index]
        keep.update(range(start, end))
    for index in range(0, recent_from):
        start, end = turns[index]
        for position in range(end - 1, start, -1):
            if _is_concluding_answer(messages[position]):
                keep.add(position)
                break

    folded: list[dict] = []
    dropped: list[dict] = []
    dropped_activity = {"read_files": [], "edited_files": [], "commands": []}

    def flush() -> None:
        if not dropped:
            return
        activity = summarize_tool_activity(dropped)
        for key in dropped_activity:
            for value in activity[key]:
                _push_unique(dropped_activity[key], value)
        responses = _preserved_responses(dropped)
        if has_tool_activity(activity) or responses:
            notice = render_dropped_work(activity, responses)
            _attach_notice(folded, notice)
            report["dropped_work_notice"] = notice
        dropped.clear()

    for index in range(count):
        if index in keep:
            flush()
            folded.append(dict(messages[index]))
        else:
            dropped.append(messages[index])
    flush()

    report["folded"] = len(folded) != count
    report["messages_after"] = len(folded)
    report["messages_removed"] = count - len(folded)
    report["dropped_work"] = dropped_activity
    return folded, report