"""Incremental task plans with stable identities and host-verified evidence links."""

from copy import deepcopy
from collections import deque
from pathlib import Path

from genesisai.shared.security import ToolError
from genesisai.shared.ids import sha


NODE_KINDS = {'phase', 'step', 'acceptance'}
NODE_STATES = {'pending', 'in_progress', 'completed', 'cancelled'}
# Discovery and protocol reads are actual evidence for preparation steps.
# Execution/registration and verification gates are checked independently.
PREPARATION_TOOLS = {'discover_tools', 'inspect_capabilities', 'search_skills', 'use_skill'}
NON_EVIDENCE_TOOLS = {'plan', 'ask_user', 'attempt_completion'}


def evidence(session, task, call_ids, *, verification=False):
    if not call_ids:
        raise ToolError('plan_evidence', '完成项需要实际工具调用证据')
    snapshots = []
    for call_id in call_ids:
        entry = session.record.calls.get(call_id)
        if call_id not in task['observed_calls'] or not entry or entry['state'] != 'succeeded':
            raise ToolError('plan_evidence', f'调用 {call_id} 不是当前任务的成功执行证据')
        result = entry.get('result') or {}
        payload = result.get('data') if isinstance(result.get('data'), dict) else {}
        if (entry['call']['name'] in NON_EVIDENCE_TOOLS or not result.get('ok') or 'completion_candidate' in result
                or payload.get('status') in {'queued', 'running', 'stopping', 'unknown', 'unknown_execution'}):
            raise ToolError('plan_evidence', f'调用 {call_id} 尚未提供实际完成结果')
        facts = deepcopy(result.get('observations') or [])
        for fact in facts:
            if fact.get('kind') in {'resource_read', 'resource_changed'} and fact.get('path'):
                path = (session.workspace / fact['path']).resolve()
                current = sha(path) if path.is_file() else None
                if current != fact.get('sha256'):
                    raise ToolError('plan_evidence', f'调用 {call_id} 的文件证据已变化，请重新核对')
                fact['path'] = str(path)
        snapshots.append({'call_id': call_id, 'tool': entry['call']['name'], 'observations': facts})
    if verification and not any(fact.get('kind') == 'verification' and fact.get('passed') is True
                                for item in snapshots for fact in item['observations']):
        raise ToolError('plan_evidence', '此验收项要求实际验证通过的工具证据')
    if verification and not any(task['call_versions'].get(item['call_id']) == task['change_revision']
                                and any(fact.get('kind') == 'verification' and fact.get('passed') is True
                                        for fact in item['observations']) for item in snapshots):
        raise ToolError('plan_evidence', '验证证据早于最新修改，请重新验证')
    return snapshots


def update(session, task, *, expected_revision, upsert=None, remove=None, non_goals=None):
    if type(expected_revision) is not int or expected_revision != task['plan_revision']:
        raise ToolError('revision_conflict', '计划版本已变化，请先用 plan 读取当前版本')
    nodes = {item['id']: deepcopy(item) for item in task['nodes']}
    changes = upsert or []
    if len({item['id'] for item in changes}) != len(changes):
        raise ToolError('plan_invalid', '一次更新不能包含重复节点 ID')
    for node_id in remove or []:
        if node_id not in nodes:
            raise ToolError('plan_invalid', f'待移除节点不存在：{node_id}')
        del nodes[node_id]
    for change in changes:
        if change.keys() - {'id', 'kind', 'text', 'parent', 'status', 'requires_verification', 'evidence_calls', 'reason'}:
            raise ToolError('plan_invalid', '计划节点包含未知字段')
        node_id = change['id']
        previous = nodes.get(node_id)
        if previous is None and (not change.get('text') or change.get('kind') not in NODE_KINDS):
            raise ToolError('plan_invalid', '新节点需要 kind 和非空 text')
        node = {**(previous or {'id': node_id, 'parent': None, 'status': 'pending',
                              'requires_verification': False, 'evidence_calls': [], 'evidence': []}), **change}
        node['parent'] = node['parent'] or None
        if not node['text'].strip() or node['kind'] not in NODE_KINDS or node['status'] not in NODE_STATES:
            raise ToolError('plan_invalid', '节点正文、类型或状态无效')
        changed_requirement = previous and any(node[key] != previous[key] for key in ('text', 'kind', 'requires_verification'))
        if changed_requirement and 'status' not in change:
            node.update(status='pending', evidence_calls=[], evidence=[])
        if node['status'] == 'completed' and node['kind'] != 'phase':
            node['evidence'] = evidence(session, task, node['evidence_calls'], verification=node['requires_verification'])
        elif node['status'] != 'completed':
            node['evidence'] = []
        nodes[node_id] = node
    visited = set()
    children = {node_id: [] for node_id in nodes}
    for node in nodes.values():
        if node['parent'] is not None and (node['parent'] not in nodes or nodes[node['parent']]['kind'] != 'phase'):
            raise ToolError('plan_invalid', '父节点必须为存在的阶段；删除阶段时显式处理其子项')
        if node['parent'] in children:
            children[node['parent']].append(node['id'])
        seen = {node['id']}
        parent = node['parent']
        while parent is not None and parent not in visited:
            if parent not in nodes or nodes[parent]['kind'] != 'phase':
                raise ToolError('plan_invalid', '父节点必须为存在的阶段；删除阶段时显式处理其子项')
            if parent in seen:
                raise ToolError('plan_invalid', '计划阶段不能循环引用')
            seen.add(parent)
            parent = nodes[parent]['parent']
        visited.update(seen)
    # A phase follows its children. New or reopened work reopens an earlier phase.
    pending = {node_id: len(child_ids) for node_id, child_ids in children.items()}
    leaves = deque(node_id for node_id, count in pending.items() if count == 0)
    while leaves:
        node_id = leaves.popleft()
        node = nodes[node_id]
        if node['kind'] == 'phase':
            states = [nodes[child]['status'] for child in children[node_id]]
            if states and all(state in {'completed', 'cancelled'} for state in states):
                node['status'] = 'completed'
            elif node['status'] == 'completed':
                node['status'] = 'pending'
        if node['parent'] is not None:
            pending[node['parent']] -= 1
            if pending[node['parent']] == 0:
                leaves.append(node['parent'])
    task['nodes'] = list(nodes.values())
    task['plan_revision'] += 1
    if non_goals is not None:
        task['non_goals'] = list(non_goals)
    task['plan_ready'] = any(item['kind'] == 'step' for item in task['nodes']) and any(item['kind'] == 'acceptance' for item in task['nodes'])
    return task


def invalidate(task, paths):
    changed = set(paths)
    reopened = set()
    for node in task['nodes']:
        observed = {item.get('path') for entry in node['evidence'] for item in entry['observations']}
        if node['status'] == 'completed' and (node['requires_verification'] or changed.intersection(observed)):
            node.update(status='pending', evidence_calls=[], evidence=[])
            reopened.add(node['id'])
    nodes = {node['id']: node for node in task['nodes']}
    for node_id in list(reopened):
        parent = nodes[node_id]['parent']
        while parent is not None and parent not in reopened:
            nodes[parent]['status'] = 'pending'
            reopened.add(parent)
            parent = nodes[parent]['parent']
    if reopened:
        task['plan_revision'] += 1


def unfinished(task):
    return [item for item in task['nodes'] if item['status'] not in {'completed', 'cancelled'}]
