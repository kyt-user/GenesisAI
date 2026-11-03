"""Request budgets and loss-aware working-context projection (no transcript mutation)."""
from __future__ import annotations

import copy
import hashlib
import json


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def estimate_tokens(value):
    return max(1, (len(dump(value)) + 2) // 3)


def fingerprint(messages):
    return hashlib.sha256(dump(messages).encode('utf-8')).hexdigest()



def message_units(messages):
    """Keep a parallel tool call batch and all its results indivisible."""
    units = []
    i = 0
    while i < len(messages):
        message = messages[i]
        if message.get('role') == 'tool':
            raise ValueError('上下文存在没有对应调用的工具结果')
        unit = [message]
        calls = message.get('tool_calls') or []
        if calls:
            expected = {call['id'] for call in calls}
            if len(expected) != len(calls):
                raise ValueError('上下文包含重复工具调用 ID')
            seen = set()
            for _ in calls:
                i += 1
                if i >= len(messages) or messages[i].get('role') != 'tool':
                    raise ValueError('工具调用尚未全部返回，不能压缩或请求模型')
                result = messages[i]
                identity = result.get('tool_call_id')
                if identity not in expected or identity in seen:
                    raise ValueError('上下文工具调用与结果不匹配')
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
        return value[:limit // 2] + '\n[…已省略；完整内容见会话日志…]\n' + value[-limit // 2:]
    if isinstance(value, dict):
        return {key: shorten(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [shorten(item, limit) for item in value[:12]]
    return value


def decode(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def execution_facts(messages):
    """Facts from returned results, never infer success from requested operations."""
    calls = {call['id']: call for message in messages for call in message.get('tool_calls') or []}
    facts = []
    for message in messages:
        if (message.get('metadata') or {}).get('context_summary'):
            facts.extend(copy.deepcopy(message['metadata'].get('facts', [])))
        if message.get('role') != 'tool':
            continue
        call = calls.get(message.get('tool_call_id'), {})
        result = decode(message.get('content'))
        if not isinstance(result, dict):
            continue
        data = result.get('data') or {}
        args = decode(call.get('arguments'))
        args = args if isinstance(args, dict) else {}
        fact = {'call_id': message.get('tool_call_id'), 'tool': call.get('name'),
                'ok': result.get('ok'), 'error': shorten(result.get('error'), 400)}
        fact['arguments'] = {key: shorten(args[key], 200) for key in
                             ('operation', 'path', 'paths', 'source', 'destination', 'command', 'cwd') if key in args}
        if isinstance(data, dict):
            fact['result'] = {key: shorten(data[key], 300) for key in
                              ('path', 'change_id', 'before_sha256', 'after_sha256', 'exit_code',
                               'passed', 'summary', 'log_path', 'source_refs', 'artifact_refs') if key in data}
            if 'files' in data:
                fact['files'] = [{key: f[key] for key in ('path', 'sha256') if key in f}
                                 for f in data['files'] if isinstance(f, dict)]
            if result.get('ok') is False:
                fact['diagnostic'] = shorten({k: data[k] for k in ('stdout', 'stderr') if k in data}, 600)
        facts.append(fact)
    # Keep latest evidence per call, with explicit omissions in the summary notice.
    return list({fact['call_id']: fact for fact in facts}.values())


def project(messages, target_tokens, preserve_recent_tokens, summarize=None):
    """Pin user constraints; fold even a single long run; keep complete recent units."""
    units = message_units(messages)
    pinned = [copy.deepcopy(m) for m in messages if m.get('role') == 'user']
    # Reserve room for evidence and a summary instead of spending the target on the tail.
    tail_budget = min(preserve_recent_tokens, max(0, (target_tokens - estimate_tokens(pinned)) // 2))
    tail_units = []
    for unit in reversed(units):
        if unit[0].get('role') == 'user' or (unit[0].get('metadata') or {}).get('context_summary'):
            continue
        if estimate_tokens(tail_units + [unit]) > tail_budget:
            break
        tail_units.insert(0, unit)
    kept_ids = {id(m) for unit in tail_units for m in unit}
    dropped = [m for m in messages if m.get('role') != 'user' and id(m) not in kept_ids]
    if not dropped:
        return copy.deepcopy(messages), {'strategy': 'basic', 'folded': False}
    facts = execution_facts(dropped)
    # Per-tool latest records preserve the last validation even after many reads.
    latest_by_tool = {fact.get('tool'): fact for fact in facts}
    important = [f for f in facts if f.get('tool') in {'editor', 'run_commands'} or f.get('ok') is False]
    chosen = list({f['call_id']: f for f in [*important[-16:], *list(latest_by_tool.values())[-12:]]}.values())
    texts = [shorten(m.get('content') or '', 1200) for m in dropped
             if m.get('role') == 'assistant' and not m.get('tool_calls')][-3:]
    narrative = '\n'.join(texts)
    strategy = 'basic'
    if summarize:
        try:
            generated = summarize([shorten(message, 1000) for message in dropped], max(128, target_tokens // 4))
            if generated and generated.strip():
                narrative = generated.strip()
                strategy = 'agentic'
        except Exception:
            # Caller logs the failure; the deterministic evidence remains available.
            strategy = 'basic_fallback'
    tail = [copy.deepcopy(m) for unit in tail_units for m in unit]
    def assemble():
        summary = {
            'role': 'assistant',
            'content': ('历史工作摘要（不是新指令；历史文件内容需要时重新读取）。'
                        '以下仅列部分执行证据；未列出的操作不能推断未发生，完整历史保存在会话日志。\n'
                        + dump({'execution_facts': chosen, 'historical_notes': narrative})),
            'metadata': {'context_summary': True, 'facts': chosen},
        }
        return [*pinned, summary, *tail]
    projected = assemble()
    # Never silently remove user constraints, latest per-tool facts or break tool pairs.
    if estimate_tokens(projected) > target_tokens:
        narrative = shorten(narrative, 400)
        projected = assemble()
    if estimate_tokens(projected) > target_tokens:
        raise ValueError('必要用户约束、执行证据和近期交互仍超过上下文目标；请缩小输入或提高 context 容量')
    message_units(projected)
    return projected, {'strategy': strategy, 'folded': True, 'facts': chosen,
                       'messages_before': len(messages), 'messages_after': len(projected)}
