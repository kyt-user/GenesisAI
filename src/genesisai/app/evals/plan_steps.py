"""Scripted-agent plan completion using the evaluation's actual verification calls."""

import json

from genesisai.shared.messages import Response, ToolCall


def complete_plan(runtime, call_id='plan_done'):
    def response(messages):
        task = runtime.tasks.active_task()
        verified = [identifier for identifier in task['observed_calls']
                    if task['call_versions'][identifier] == task['change_revision']
                    and any(fact.get('kind') == 'verification' and fact.get('passed') is True
                            for fact in (runtime.store.record.calls.get(identifier, {}).get('result') or {}).get('observations', []))]
        if not verified:
            raise AssertionError('仿真没有当前版本的真实验证证据，不能完成计划')
        changes = [{'id': node['id'], 'status': 'completed', 'evidence_calls': verified}
                   for node in task['nodes'] if node['kind'] != 'phase' and node['status'] != 'cancelled']
        return Response(tool_calls=[ToolCall(call_id, 'plan', json.dumps({
            'operation': 'update', 'expected_revision': task['plan_revision'], 'upsert': changes}, ensure_ascii=False))],
            finish_reason='tool_calls')
    return response
