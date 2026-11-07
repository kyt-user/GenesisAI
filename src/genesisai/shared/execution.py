"""Bounded execution facts derived from the call ledger, never model narration."""

import json


def _bounded(value):
    if isinstance(value, str):
        return value if len(value) <= 300 else value[:300] + '…[已截断，详见调用日志]'
    if isinstance(value, list):
        return [_bounded(item) for item in value[:8]] + (['…[其余项见调用日志]'] if len(value) > 8 else [])
    if isinstance(value, dict):
        return {key: _bounded(item) for key, item in value.items()}
    return value


def execution_facts(record, activities, *, limit=6):
    facts = []
    for activity in activities[-limit:]:
        call_id = activity.get("call_id")
        entry = record.calls.get(call_id)
        if not entry:
            continue
        call = entry["call"]
        try:
            args = json.loads(call["arguments"])
        except (TypeError, ValueError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        result = entry.get("result") or {}
        data = result.get("data") or {}
        data = data if isinstance(data, dict) else {}
        error = result.get("error") or {}
        facts.append(dict(
            call_id=call_id, tool=call["name"], state=entry["state"],
            executor_started=entry.get("executed", False), ok=result.get("ok"),
            arguments={k: args[k] for k in ("operation", "command", "cwd", "path", "paths") if k in args},
            result={k: data[k] for k in ("command", "cwd", "exit_code", "passed", "log_path") if k in data},
            error={k: error[k] for k in ("code", "message") if k in error},
        ))
    return _bounded(facts)
