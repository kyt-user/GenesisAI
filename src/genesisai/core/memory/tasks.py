"""Durable objectives, user constraints, incremental plans and observed evidence."""

import json
from pathlib import Path
import time

from genesisai.core.persistence.database import encode
from genesisai.core.memory import task_plan
from genesisai.shared.ids import uid, sha
from genesisai.shared.security import ToolError


TERMINAL = {'completed', 'cancelled', 'superseded'}
INTENT_FIELDS = ('profile', 'protocols', 'capability_routes', 'capability_goal', 'capability_target')


class TaskService:
    def __init__(self, session):
        self.session = session
        self.database = session.database

    def active_task(self):
        rows = self.database.rows('SELECT payload FROM tasks WHERE session_id=? ORDER BY updated DESC LIMIT 1', (self.session.id,))
        if not rows:
            return None
        task = json.loads(rows[0]['payload'])
        if task.get('schema_version') != 2:
            raise ValueError('任务记录格式不受当前版本支持；原记录保留，请使用新会话')
        return task

    def _put(self, task):
        task = self.session.get_logger().journal.clean(task)
        previous = task['revision']
        task.update(revision=previous + 1, updated_at=time.time())
        with self.database.transaction() as db:
            if previous == 0:
                db.execute('INSERT INTO tasks VALUES(?,?,?,?)', (task['id'], self.session.id, encode(task), task['updated_at']))
            else:
                result = db.execute("UPDATE tasks SET payload=?,updated=? WHERE id=? AND json_extract(payload,'$.revision')=?",
                                    (encode(task), task['updated_at'], task['id'], previous))
                if result.rowcount != 1:
                    raise ToolError('task_revision_conflict', '任务状态已被另一执行器更新，请重新读取')
        return task

    def begin_task(self, objective, *, run_id=None, instruction=None, new_task=False,
                   constraint_changes=None, execution_requested=True):
        self.reconcile()
        task = self.active_task()
        if task and new_task and task['status'] not in TERMINAL:
            task['status'] = 'superseded'
            task = self._put(task)
        if not task or task['status'] in TERMINAL or new_task:
            task = dict(schema_version=2, id=uid('task'), revision=0, objective=str(objective), status='planning',
                        run_ids=[], instructions=[], constraints=[], execution_requested=bool(execution_requested),
                        plan_revision=0, nodes=[], non_goals=[], plan_ready=False,
                        verification_passed=False, verification_required=False, verification_revision=-1,
                        observed_files={}, changed_files={}, drifted_files=[],
                        last_write_at=None, last_verification_at=None, change_revision=0,
                        observations=[], observed_calls=[], call_versions={}, handoff=None, execution_context={})
        fresh_run = run_id is None or run_id not in task['run_ids']
        if run_id and fresh_run:
            task['run_ids'].append(run_id)
        if fresh_run:
            source = {'id': uid('instruction'), 'run_id': run_id, 'text': instruction if instruction is not None else str(objective),
                      'created_at': time.time()}
            task['instructions'].append(source)
            for change in constraint_changes or []:
                key, value, scope = change['key'], change['value'], change.get('scope', 'task')
                if key not in {'read_only', 'network_forbidden'} or type(value) is not bool or scope not in {'task', 'turn'}:
                    raise ValueError('用户约束字段、值或作用域无效')
                for old in task['constraints']:
                    if old['key'] == key and old['scope'] == scope and old['status'] == 'active':
                        old.update(status='revoked', revoked_by=source['id'], revoked_at=time.time())
                task['constraints'].append(dict(id=uid('constraint'), key=key, value=value, scope=scope,
                                                expires='task_end' if scope == 'task' else 'turn_end', status='active',
                                                source_id=source['id'], source_run_id=run_id))
        task['execution_requested'] |= bool(execution_requested)
        if execution_requested or task['revision'] == 0:
            task['execution_context'].update({key: self.session.record.run_runtime[key] for key in INTENT_FIELDS
                                              if key in self.session.record.run_runtime})
        return self._put(task)

    @staticmethod
    def effective_constraints(task, run_id):
        values = {'read_only': False, 'network_forbidden': False}
        if task['status'] not in TERMINAL:
            for item in task['constraints']:
                if item['status'] == 'active' and (item['scope'] == 'task' or item['source_run_id'] == run_id):
                    values[item['key']] = item['value']
        return values

    def update_constraints(self, changes, *, source='cli'):
        """Apply an explicit user transition to the current task constraints."""
        if (source != 'cli' or not isinstance(changes, dict) or not changes
                or changes.keys() - {'read_only', 'network_forbidden'}
                or any(type(value) is not bool for value in changes.values())):
            raise ToolError('validation_error', '任务约束只接受 read_only/network_forbidden 布尔值')
        task = self.active_task()
        if not task or task['status'] in TERMINAL:
            raise ToolError('project_task_missing', '约束修改需要当前活动任务')
        run_id = self.session.record.run_id
        origin = dict(id=uid('instruction'), run_id=run_id, source=source,
                      text=encode({'constraints': changes, 'scope': 'task'}), created_at=time.time())
        task['instructions'].append(origin)
        for key, value in changes.items():
            for old in task['constraints']:
                if old['key'] == key and old['scope'] == 'task' and old['status'] == 'active':
                    old.update(status='revoked', revoked_by=origin['id'], revoked_at=time.time())
            task['constraints'].append(dict(id=uid('constraint'), key=key, value=value, scope='task',
                expires='task_end', status='active', source_id=origin['id'], source_run_id=run_id))
        task = self._put(task)
        effective = self.effective_constraints(task, run_id)
        self.session.record.run_runtime['constraints'] = effective
        self.session.commit()
        return {'task_id': task['id'], 'constraints': effective, 'history': task['constraints']}

    def update_plan(self, *, expected_revision, upsert=None, remove=None, non_goals=None):
        self.reconcile()
        task = self.active_task()
        if not task or task['status'] in TERMINAL:
            raise ToolError('project_task_missing', '当前没有可更新的活动任务')
        task = task_plan.update(self.session, task, expected_revision=expected_revision, upsert=upsert,
                               remove=remove, non_goals=non_goals)
        task['status'] = 'in_progress' if task['last_write_at'] else 'planned'
        task = self._put(task)
        self.session.get_logger().record('task_plan_update', task_id=task['id'], plan_revision=task['plan_revision'],
                                         upsert=upsert or [], remove=remove or [], non_goals=non_goals)
        return task

    def observe(self, observations, *, call_id=None):
        task = self.active_task()
        if not task or task['status'] in TERMINAL:
            return
        if self.session.record.run_id and self.session.record.run_id not in task['run_ids']:
            return
        if call_id and call_id in task['observed_calls']:
            return
        changed = []
        now = time.time()
        for item in observations:
            task['observations'].append({'call_id': call_id, 'at': now, **item})
            if item['kind'] in {'resource_read', 'resource_changed'}:
                path = (self.session.workspace / item['path']).resolve()
                if path.is_relative_to(self.session.workspace):
                    name = str(path)
                    if (item['kind'] == 'resource_read' and name in task['observed_files']
                            and task['observed_files'][name] != item.get('sha256')):
                        required = task['verification_required'] or bool(task['last_write_at']) or task['verification_passed']
                        task.update(verification_passed=False, verification_required=required,
                                    status='needs_verification' if required else 'in_progress')
                        task['change_revision'] += 1
                        task['changed_files'][name] = item.get('sha256')
                        changed.append(name)
                    task['observed_files'][str(path)] = item.get('sha256')
            if item['kind'] == 'resource_changed':
                changed.append(str(path))
                task['changed_files'][str(path)] = item.get('sha256')
                task.update(last_write_at=now, verification_passed=False, status='in_progress')
                task['change_revision'] += 1
            if item['kind'] == 'verification':
                task.update(last_verification_at=now, verification_passed=item['passed'],
                            verification_revision=task['change_revision'],
                            status='in_progress' if item['passed'] else 'needs_verification')
        if changed:
            task_plan.invalidate(task, changed)
        verified = [item for item in observations if item['kind'] == 'verification']
        if verified:
            task['verification_required'] = not verified[-1]['passed']
        elif changed:
            task['verification_required'] |= bool(self.session.record.run_runtime.get('verification_required'))
        if call_id:
            task['observed_calls'].append(call_id)
            task['call_versions'][call_id] = task['change_revision']
        if self.session.record.run_runtime.get('execution_requested'):
            task['execution_context'].update({key: self.session.record.run_runtime[key] for key in INTENT_FIELDS
                                              if key in self.session.record.run_runtime})
        self._put(task)

    def reconcile(self):
        task = self.active_task()
        if not task or task['status'] in TERMINAL:
            return []
        current = {name: sha(Path(name)) if Path(name).is_file() else None for name in task['observed_files']}
        drifted = [name for name, digest in task['observed_files'].items() if current[name] != digest]
        if drifted or task['drifted_files']:
            task['drifted_files'] = drifted
            if drifted:
                needs_verification = task['verification_required'] or bool(task['last_write_at']) or task['verification_passed']
                task.update(verification_passed=False, verification_required=needs_verification,
                            status='needs_verification' if needs_verification else 'in_progress')
                task['change_revision'] += 1
                task['observed_files'].update(current)
                task['changed_files'].update({name: current[name] for name in drifted})
                task_plan.invalidate(task, drifted)
            self._put(task)
        return drifted

    def completion_issue(self):
        self.reconcile()
        task = self.active_task()
        if not task or self.session.record.run_id not in task['run_ids']:
            return None
        if task['verification_required']:
            return '当前修改或测试仍有未通过的验证，请修复并按实际结果验证；上一轮失败不会因新消息自动清除。'
        if not task['plan_revision']:
            return None
        if not task['plan_ready']:
            return '当前计划缺少实施步骤或验收项；请用 plan 补全，不得以删除计划代替完成任务。'
        remaining = task_plan.unfinished(task)
        if remaining:
            return '计划还有未完成项：' + ', '.join(item['id'] for item in remaining[:8]) + '。用 plan 分页查看，按实际工具证据更新完成项。'
        return None

    def has_execution_evidence(self):
        task = self.active_task()
        if not task or self.session.record.run_id not in task['run_ids']:
            return False
        for call_id in reversed(task['observed_calls']):
            if (self.session.record.calls.get(call_id, {}).get('call') or {}).get('name') in task_plan.PREPARATION_TOOLS:
                continue
            try:
                task_plan.evidence(self.session, task, [call_id])
                return True
            except ToolError:
                continue
        return False

    def finish(self, status, answer=''):
        task = self.active_task()
        if not task or self.session.record.run_id not in task['run_ids']:
            return
        if status == 'cancelled':
            task['status'] = 'cancelled'
        elif status == 'completed':
            valid = not task['verification_required'] and not task['drifted_files']
            discussion = task['execution_requested'] and not self.session.record.run_runtime.get('execution_requested')
            task['status'] = ('completed' if valid and not discussion and not task_plan.unfinished(task)
                              else 'in_progress' if valid else 'needs_verification')
        elif status == 'failed':
            task['status'] = 'blocked'
        task['handoff'] = {'run_id': self.session.record.run_id, 'outcome': status, 'answer': answer,
                           'pending_ids': [item['id'] for item in task_plan.unfinished(task)],
                           'verification': {'passed': task['verification_passed'], 'at': task['last_verification_at'],
                                            'change_revision': task['change_revision']}}
        task = self._put(task)
        self.session.record.run_runtime.update(project_task_id=task['id'], project_task_status=task['status'])
        if task['status'] in TERMINAL:
            self.session.record.run_runtime['constraints'] = self.effective_constraints(task, self.session.record.run_id)

    def read(self, *, section='plan', offset=0, limit=20, parent=None, status=None, max_chars=4000):
        task = self.active_task()
        if not task:
            raise ToolError('project_task_missing', '当前没有活动任务')
        metadata = {key: task[key] for key in ('id', 'status', 'plan_revision', 'plan_ready')}
        metadata['objective_chars'] = len(task['objective'])
        metadata['objective_read'] = {'operation': 'read', 'section': 'objective'}
        if section in {'objective', 'handoff'}:
            text = task['objective'] if section == 'objective' else json.dumps(task['handoff'], ensure_ascii=False)
            content = text[offset:offset + max_chars]
            return metadata | {'text': content, 'total_chars': len(text),
                    'next_offset': offset + len(content) if offset + len(content) < len(text) else None}
        records = task['nodes'] if section == 'plan' else task[section]
        if section == 'plan':
            records = [item for item in records if (parent is None or item['parent'] == parent)
                       and (status is None or item['status'] == status)]
        page = records[offset:offset + limit]
        return {**metadata, 'items': page, 'total': len(records),
                'next_offset': offset + len(page) if offset + len(page) < len(records) else None}

    def context(self):
        self.reconcile()
        task = self.active_task()
        if not task:
            return {}
        pending = task_plan.unfinished(task)
        return {key: task[key] for key in ('id', 'objective', 'status', 'plan_revision', 'non_goals', 'drifted_files')} | {
            'participating': self.session.record.run_id in task['run_ids'],
            'constraints': [item for item in task['constraints'] if task['status'] not in TERMINAL
                            and item['status'] == 'active' and (item['scope'] == 'task'
                            or item['source_run_id'] == self.session.record.run_id)],
            'pending_count': len(pending), 'pending': pending[:12],
            'verification_passed': task['verification_passed'],
            'read_more': {'tool': 'plan', 'operation': 'read', 'sections': ['plan', 'instructions', 'constraints', 'objective', 'handoff']},
            'handoff': None if task['handoff'] is None else {key: task['handoff'][key] for key in ('run_id', 'outcome', 'verification')},
        }
