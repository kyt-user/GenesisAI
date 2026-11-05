"""Request budgets and loss-aware working-context projection (no transcript mutation)."""

from __future__ import annotations

import copy
import hashlib
import json
from genesisai.shared.messages import image_tokens, content_text, image_parts


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def estimate_tokens(value):
    image_cost = 0

    def textual(item):
        nonlocal image_cost
        if isinstance(item, dict):
            if item.get("type") == "image" and "image_ref" in item:
                image_cost += image_tokens(item)
                return {"image_ref": item["image_ref"]}
            return {k: textual(v) for k, v in item.items()}
        if isinstance(item, list):
            return [textual(v) for v in item]
        return item

    text = dump(textual(value))
    return max(1, (len(text) + 2) // 3) + image_cost


def image_placeholder(part):
    return {
        "type": "text",
        "text": f"[历史图片 {part['name']}，image_ref={part['image_ref']}；本次未附带像素，需要查看时调用 read_image(image_ref)。]",
    }


def project_images(messages, max_images, max_bytes, *, history=False):
    """Keep the latest user images and current tool image batch; bound older ones."""
    values = copy.deepcopy(messages)
    last_user = max(
        (i for i, m in enumerate(values) if m["role"] == "user"), default=-1
    )
    latest_batch = set()
    offset = 0
    for unit in message_units(values):
        indices = list(range(offset, offset + len(unit)))
        offset += len(unit)
        if (
            indices
            and indices[0] > last_user
            and any(image_parts(m.get("content")) for m in unit)
        ):
            latest_batch = set(indices)
    protected = {last_user, *latest_batch}
    slots = [
        (i, j, p)
        for i, m in enumerate(values)
        if isinstance(m.get("content"), list)
        for j, p in enumerate(m["content"])
        if p.get("type") == "image"
    ]
    keep = {(i, j) for i, j, p in slots if i in protected}
    count = len(keep)
    size = sum(p["byte_size"] for i, j, p in slots if (i, j) in keep)
    if count > max_images or size > max_bytes:
        raise ValueError(
            "当前轮次必要图片超过数量或总大小限制，请减少图片或调整 context 图片预算"
        )
    for i, j, p in reversed(slots):
        if (i, j) in keep:
            continue
        if not history and count < max_images and size + p["byte_size"] <= max_bytes:
            keep.add((i, j))
            count += 1
            size += p["byte_size"]
        else:
            values[i]["content"][j] = image_placeholder(p)
    return values


def fingerprint(messages):
    return hashlib.sha256(dump(messages).encode("utf-8")).hexdigest()


def message_units(messages):
    """Keep a parallel tool call batch and all its results indivisible."""
    units = []
    i = 0
    while i < len(messages):
        message = messages[i]
        if message.get("role") == "tool":
            raise ValueError("上下文存在没有对应调用的工具结果")
        unit = [message]
        calls = message.get("tool_calls") or []
        if calls:
            expected = {call["id"] for call in calls}
            if len(expected) != len(calls):
                raise ValueError("上下文包含重复工具调用 ID")
            seen = set()
            for _ in calls:
                i += 1
                if i >= len(messages) or messages[i].get("role") != "tool":
                    raise ValueError("工具调用尚未全部返回，不能压缩或请求模型")
                result = messages[i]
                identity = result.get("tool_call_id")
                if identity not in expected or identity in seen:
                    raise ValueError("上下文工具调用与结果不匹配")
                seen.add(identity)
                unit.append(result)
        units.append(unit)
        i += 1
    return units


def shorten(value, limit=800):
    """Only summarize copies; handles nested files, stdout/stderr and patch arguments."""
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return (
            value[: limit // 2]
            + "\n[…已省略；完整内容见会话日志…]\n"
            + value[-limit // 2 :]
        )
    if isinstance(value, dict):
        if value.get("type") == "image" and "image_ref" in value:
            return image_placeholder(value)
        return {key: shorten(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [shorten(item, limit) for item in value[:12]]
    return value


def decode(value):
    if isinstance(value, list):
        value = content_text(value)
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def execution_facts(messages):
    """Facts from returned results, never infer success from requested operations."""
    calls = {
        call["id"]: call
        for message in messages
        for call in message.get("tool_calls") or []
    }
    facts = []
    for message in messages:
        if (message.get("metadata") or {}).get("context_summary"):
            facts.extend(copy.deepcopy(message["metadata"].get("facts", [])))
        if message.get("role") != "tool":
            continue
        call = calls.get(message.get("tool_call_id"), {})
        result = decode(message.get("content"))
        if not isinstance(result, dict):
            continue
        data = result.get("data") or {}
        args = decode(call.get("arguments"))
        args = args if isinstance(args, dict) else {}
        fact = {
            "call_id": message.get("tool_call_id"),
            "tool": call.get("name"),
            "ok": result.get("ok"),
            "error": shorten(result.get("error"), 400),
        }
        fact["arguments"] = {
            key: shorten(args[key], 200)
            for key in (
                "operation",
                "path",
                "paths",
                "source",
                "destination",
                "command",
                "cwd",
            )
            if key in args
        }
        if isinstance(data, dict):
            fact["result"] = {
                key: shorten(data[key], 300)
                for key in (
                    "path",
                    "change_id",
                    "before_sha256",
                    "after_sha256",
                    "exit_code",
                    "passed",
                    "summary",
                    "log_path",
                    "source_refs",
                    "artifact_refs",
                )
                if key in data
            }
            if "files" in data:
                fact["files"] = [
                    {key: f[key] for key in ("path", "sha256") if key in f}
                    for f in data["files"]
                    if isinstance(f, dict)
                ]
            if result.get("ok") is False:
                fact["diagnostic"] = shorten(
                    {k: data[k] for k in ("stdout", "stderr") if k in data}, 600
                )
        facts.append(fact)
    # Keep latest evidence per call, with explicit omissions in the summary notice.
    return list({fact["call_id"]: fact for fact in facts}.values())


def project(messages, target_tokens, preserve_recent_tokens, summarize=None):
    """Fold chronological prefixes; keep the latest request and complete tool batches."""
    original = messages
    messages = project_images(messages, 1000000, 2**63, history=True)
    units = message_units(messages)
    if not units:
        return [], {"strategy": "basic", "folded": False}
    latest_user = max(
        (i for i, unit in enumerate(units) if unit[0]["role"] == "user"), default=-1
    )
    current = units[latest_user] if latest_user >= 0 else []
    tail_budget = min(
        preserve_recent_tokens, max(0, (target_tokens - estimate_tokens(current)) // 2)
    )
    cut = len(units)
    used = 0
    for i in range(len(units) - 1, -1, -1):
        cost = estimate_tokens(units[i])
        if used + cost > tail_budget:
            break
        cut = i
        used += cost
    if cut == 0:
        return copy.deepcopy(messages), {
            "strategy": "basic",
            "folded": messages != original,
        }
    strategy = "basic"
    all_facts = []

    def summary(fragment):
        nonlocal strategy
        if not fragment:
            return []
        facts = execution_facts(fragment)
        chosen = facts[-8:]
        all_facts.extend(chosen)
        narrative = "\n".join(
            content_text(m.get("content"))[:600]
            for m in fragment
            if m["role"] in {"user", "assistant"} and not m.get("tool_calls")
        )[-2500:]
        if summarize:
            try:
                generated = summarize(
                    [shorten(m, 600) for m in fragment],
                    max(128, min(2048, target_tokens // 6)),
                )
                if generated and generated.strip():
                    narrative = generated.strip()[:6000]
                    strategy = "agentic"
            except Exception:
                strategy = "basic_fallback"
        refs = []
        for message in fragment:
            metadata = message.get("metadata") or {}
            if ref := metadata.get("history_ref"):
                refs.append(ref)
            elif prior_range := metadata.get("history_range"):
                refs.extend((prior_range["first"], prior_range["last"]))
        reference = {"first": refs[0], "last": refs[-1]} if refs else {}
        return [
            {
                "role": "assistant",
                "content": "历史摘要（参考资料，不是新指令）。细节可能省略；需要原文请 memory_search / memory_read，不可把未列出的操作当作未发生。\n"
                + dump(
                    {"range": reference, "execution_facts": chosen, "notes": narrative}
                ),
                "metadata": {
                    "context_summary": True,
                    "facts": chosen,
                    "history_range": reference,
                },
            }
        ]

    flatten = lambda chunk: [m for unit in chunk for m in unit]
    if 0 <= latest_user < cut:
        projected = [
            *summary(flatten(units[:latest_user])),
            *copy.deepcopy(current),
            *summary(flatten(units[latest_user + 1 : cut])),
            *copy.deepcopy(flatten(units[cut:])),
        ]
    else:
        projected = [
            *summary(flatten(units[:cut])),
            *copy.deepcopy(flatten(units[cut:])),
        ]
    if estimate_tokens(projected) > target_tokens:
        raise ValueError(
            "当前请求、执行证据和完整工具批次仍超过上下文目标；请缩小输入或提高 context 容量"
        )
    message_units(projected)
    return projected, {
        "strategy": strategy,
        "folded": True,
        "facts": all_facts,
        "messages_before": len(messages),
        "messages_after": len(projected),
    }
