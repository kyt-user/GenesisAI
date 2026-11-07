import json

import pytest

from genesisai.core.memory import TaskService
from genesisai.core.persistence.session import Session
from genesisai.shared.security import ToolError
from test_development_continuation import development_env


def test_full_objective_and_handoff_survive_reopen(tmp_path):
    session = Session(tmp_path / 'state', workspace=tmp_path)
    manager = TaskService(session)
    objective = '项目要求。' * 100 + '最后必须审计并交付。'
    initial = manager.begin_task(objective, run_id='first', constraint_changes=[{'key': 'network_forbidden', 'value': True}])
    session.record.run_id = 'first'
    session.commit()
    manager.finish('partial', '交接事项。' * 1200 + '最后的未完成项')
    restored = TaskService(Session(session.root, session.id))
    resumed = restored.begin_task('继续', run_id='second', instruction='增加负数用例')
    assert resumed['id'] == initial['id'] and resumed['objective'] == objective
    assert resumed['handoff']['answer'].endswith('最后的未完成项')
    assert restored.effective_constraints(resumed, 'second')['network_forbidden']
    assert resumed['instructions'][-1]['text'] == '增加负数用例'


def test_scoped_constraints_require_explicit_revocation(tmp_path):
    manager = TaskService(Session(tmp_path / 'state', workspace=tmp_path))
    first = manager.begin_task('修改项目', run_id='one', constraint_changes=[
        {'key': 'network_forbidden', 'value': True}, {'key': 'read_only', 'value': True, 'scope': 'turn'}])
    assert manager.effective_constraints(first, 'one') == {'network_forbidden': True, 'read_only': True}
    second = manager.begin_task('继续', run_id='two')
    assert manager.effective_constraints(second, 'two') == {'network_forbidden': True, 'read_only': False}
    manager.session.record.run_id = 'two'
    assert all(item['key'] != 'read_only' for item in manager.context()['constraints'])
    third = manager.begin_task('现在允许联网', run_id='three', constraint_changes=[{'key': 'network_forbidden', 'value': False}])
    assert not manager.effective_constraints(third, 'three')['network_forbidden']
    revoked = next(item for item in third['constraints'] if item['key'] == 'network_forbidden')
    assert revoked['status'] == 'revoked' and revoked['revoked_by'] == third['instructions'][-1]['id']


def test_passing_verification_does_not_finish_uncompleted_goal(tmp_path):
    _, manager, task, store, runtime = development_env(tmp_path)
    manager.observe([{'kind': 'verification', 'passed': True}])
    restored = TaskService(Session(store.root, store.id))
    current = restored.begin_task('继续其余阶段', run_id='next')
    assert current['id'] == task['id'] and current['status'] == 'in_progress'
    store.record.run_id = 'next'
    assert manager.completion_issue() is not None
    assert current['verification_passed']
    runtime.close()


def test_completed_goal_stays_closed_after_external_file_change(tmp_path):
    from genesisai.shared.ids import sha
    session = Session(tmp_path / 'state', workspace=tmp_path)
    session.record.run_id = 'one'
    session.record.run_runtime = {'execution_requested': True}
    manager = TaskService(session)
    first = manager.begin_task('核对文件', run_id='one')
    target = tmp_path / 'source.py'
    target.write_text('before', encoding='utf-8')
    manager.observe([{'kind': 'resource_read', 'path': str(target), 'sha256': sha(target)}], call_id='read')
    manager.finish('completed', '已核对。')
    target.write_text('later', encoding='utf-8')
    assert manager.context()['status'] == 'completed'
    second = manager.begin_task('新的核对事项', run_id='two')
    assert second['id'] != first['id']
    assert second['objective'] == '新的核对事项'


def test_incremental_plan_paging_cycles_and_revision_conflicts(tmp_path):
    manager = TaskService(Session(tmp_path / 'state', workspace=tmp_path))
    manager.begin_task('大型项目')
    nodes = [{'id': 'phase', 'kind': 'phase', 'text': '实施'}]
    nodes += [{'id': f'step_{i}', 'kind': 'step', 'text': '工作项' * 300, 'parent': 'phase'} for i in range(80)]
    nodes += [{'id': 'accept', 'kind': 'acceptance', 'text': '通过测试', 'requires_verification': True}]
    manager.update_plan(expected_revision=0, upsert=nodes)
    page = manager.read(offset=70, limit=20)
    assert page['total'] == 82 and any(item['id'] == 'step_79' for item in page['items'])
    assert page['next_offset'] is None
    manager.update_plan(expected_revision=1, upsert=[{'id': 'step_79', 'text': '只修订尾项'}])
    assert manager.read(offset=80, limit=1)['items'][0]['text'] == '只修订尾项'
    with pytest.raises(ToolError, match='版本'):
        manager.update_plan(expected_revision=1, upsert=[])
    with pytest.raises(ToolError, match='循环'):
        manager.update_plan(expected_revision=2, upsert=[{'id': 'phase', 'parent': 'phase'}])
    assert manager.active_task()['plan_revision'] == 2


def test_plan_completion_needs_actual_current_evidence(tmp_path):
    _, manager, _, store, runtime = development_env(tmp_path)
    store.record.run_id = 'run_fixture'
    manager.begin_task('继续', run_id=store.record.run_id)
    update = [{'id': 'accept', 'status': 'completed', 'evidence_calls': ['invented']}]
    with pytest.raises(ToolError, match='成功执行证据'):
        manager.update_plan(expected_revision=1, upsert=update)
    store.record.calls['proposal'] = {'call': {'name': 'custom_final_tool'}, 'state': 'succeeded',
                                     'result': {'ok': True, 'data': {}, 'completion_candidate': '全部完成。'}}
    manager.observe([], call_id='proposal')
    with pytest.raises(ToolError, match='实际完成结果'):
        manager.update_plan(expected_revision=1, upsert=[{'id': 'create', 'status': 'completed', 'evidence_calls': ['proposal']}])
    store.record.calls['check'] = {'call': {'name': 'run_commands'}, 'state': 'succeeded',
                                  'result': {'ok': True, 'data': {}, 'observations': [{'kind': 'verification', 'passed': True}]}}
    manager.observe([{'kind': 'verification', 'passed': True}], call_id='check')
    manager.update_plan(expected_revision=1, upsert=[{'id': 'accept', 'status': 'completed', 'evidence_calls': ['check']}])
    target = store.workspace / 'new.py'
    target.write_text('x=1', encoding='utf-8')
    from genesisai.shared.ids import sha
    manager.observe([{'kind': 'resource_changed', 'path': str(target), 'sha256': sha(target)}], call_id='write')
    task = manager.active_task()
    assert next(item for item in task['nodes'] if item['id'] == 'accept')['status'] == 'pending'
    with pytest.raises(ToolError, match='早于最新修改'):
        manager.update_plan(expected_revision=task['plan_revision'], upsert=[{'id': 'accept', 'status': 'completed', 'evidence_calls': ['check']}])
    runtime.close()


def test_status_turn_keeps_objective_and_explicit_constraint_change_persists(tmp_path):
    from genesisai.agent.loop import Runner
    from genesisai.shared.messages import Response
    from test_acceptance import FakeModel
    _, manager, initial, store, runtime = development_env(tmp_path, planned=False)
    manager.begin_task('补充约束', constraint_changes=[{'key': 'network_forbidden', 'value': True}])
    assert Runner(FakeModel(Response(content='仍有待完成工作。')), runtime).start('进度怎么样')['status'] == 'completed'
    task = manager.active_task()
    assert task['id'] == initial['id'] and task['status'] != 'completed'
    assert store.record.run_runtime['objective'] == initial['objective']
    assert store.record.run_runtime['constraints']['network_forbidden']
    Runner(FakeModel(Response(content='已记录联网许可。')), runtime).start('现在允许联网')
    restored = TaskService(Session(store.root, store.id))
    assert not restored.effective_constraints(restored.active_task(), store.record.run_id)['network_forbidden']
    assert restored.active_task()['id'] == initial['id']
    runtime.close()


def test_relative_path_evidence_invalidates_after_file_changes(tmp_path):
    from genesisai.shared.ids import sha
    workspace, manager, _, store, runtime = development_env(tmp_path)
    store.record.run_id = 'one'
    manager.begin_task('继续', run_id='one')
    target = workspace / 'index.html'
    target.write_text('first', encoding='utf-8')
    fact = {'kind': 'resource_read', 'path': 'index.html', 'sha256': sha(target)}
    store.record.calls['read'] = {'call': {'name': 'read_files'}, 'state': 'succeeded',
                                 'result': {'ok': True, 'data': {}, 'observations': [fact]}}
    manager.observe([fact], call_id='read')
    manager.update_plan(expected_revision=1, upsert=[{'id': 'create', 'status': 'completed', 'evidence_calls': ['read']}])
    assert store.record.calls['read']['result']['observations'][0]['path'] == 'index.html'
    target.write_text('second', encoding='utf-8')
    manager.reconcile()
    assert next(node for node in manager.active_task()['nodes'] if node['id'] == 'create')['status'] == 'pending'
    runtime.close()


def test_repeated_external_changes_and_reads_invalidate_verification(tmp_path):
    from genesisai.shared.ids import sha
    workspace, manager, _, store, runtime = development_env(tmp_path, planned=False)
    store.record.run_id = 'one'
    manager.begin_task('继续', run_id='one')
    target = workspace / 'source.py'
    target.write_text('first', encoding='utf-8')
    manager.observe([{'kind': 'resource_read', 'path': 'source.py', 'sha256': sha(target)},
                     {'kind': 'verification', 'passed': True}], call_id='initial')
    target.write_text('second', encoding='utf-8')
    manager.reconcile()
    first_revision = manager.active_task()['change_revision']
    manager.observe([{'kind': 'verification', 'passed': True}], call_id='verify_second')
    target.write_text('third', encoding='utf-8')
    manager.reconcile()  # Same drifted path as last time, but a new content revision.
    task = manager.active_task()
    assert task['change_revision'] > first_revision and task['verification_required']
    manager.observe([{'kind': 'verification', 'passed': True}], call_id='verify_third')
    target.write_text('fourth', encoding='utf-8')
    manager.observe([{'kind': 'resource_read', 'path': 'source.py', 'sha256': sha(target)}], call_id='read_fourth')
    task = manager.active_task()
    assert task['verification_required'] and not task['verification_passed']
    runtime.close()


def test_deep_phase_tree_has_no_recursion_or_fixed_plan_size_limit(tmp_path):
    manager = TaskService(Session(tmp_path / 'state', workspace=tmp_path))
    manager.begin_task('分阶段项目')
    nodes = [{'id': str(i), 'kind': 'phase', 'text': f'阶段{i}', 'parent': str(i - 1) if i else ''} for i in range(1100)]
    nodes += [{'id': 'step', 'kind': 'step', 'text': '最后一项', 'parent': '1099'},
              {'id': 'accept', 'kind': 'acceptance', 'text': '最终验收'}]
    manager.update_plan(expected_revision=0, upsert=nodes)
    assert manager.read(offset=1100)['items'][0]['text'] == '最后一项'
    with pytest.raises(ToolError, match='循环'):
        manager.update_plan(expected_revision=1, upsert=[{'id': '0', 'parent': '1099'}])


@pytest.mark.parametrize('text', ['不是新任务，继续实现', '请解释什么是新任务', '继续实现，这不是新任务'])
def test_mentioning_new_task_does_not_replace_the_goal(text):
    from genesisai.core.prompt.task_protocols import TaskProtocolSelector
    assert not TaskProtocolSelector().route(text)['new_task']


@pytest.mark.parametrize('text', ['新任务：分析日志', '接下来开始新任务，检查代码', 'New task: inspect logs'])
def test_explicit_new_task_can_replace_the_goal(text):
    from genesisai.core.prompt.task_protocols import TaskProtocolSelector
    assert TaskProtocolSelector().route(text)['new_task']


@pytest.mark.parametrize('text', ['请创建可复用 Tool，名称 double_integer', '创建可重复使用的工具', 'Build a reusable tool for adding numbers'])
def test_explicit_reusable_tool_request_selects_builder_protocol(text):
    from genesisai.core.prompt.task_protocols import TaskProtocolSelector
    route = TaskProtocolSelector().route(text)
    assert 'capability_builder' in route['protocols']
    assert route['execution_requested']


def test_explaining_capability_creation_is_not_an_execution_request():
    from genesisai.core.prompt.task_protocols import TaskProtocolSelector
    assert not TaskProtocolSelector().route('解释如何创建可复用 Tool')['execution_requested']
    assert 'capability_builder' not in TaskProtocolSelector().route('修复项目里的工具函数')['protocols']


def test_new_message_cannot_clear_a_failed_verification(tmp_path):
    from genesisai.agent.loop import Runner
    from genesisai.shared.messages import Response, ToolCall
    from test_acceptance import FakeModel
    _, manager, initial, store, runtime = development_env(tmp_path, planned=False)
    (store.workspace / 'test_case.py').write_text('def test_case(): assert False\n', encoding='utf-8')
    first = FakeModel(Response(tool_calls=[ToolCall('actual_test', 'run_commands', '{}')]),
                      Response(content='[[PARTIAL]] 实际测试失败，尚未修复。'))
    assert Runner(first, runtime).start('继续实现')['status'] == 'partial'
    assert manager.active_task()['verification_required']
    second = FakeModel(*[Response(content='已全部完成。') for _ in range(4)])
    result = Runner(second, runtime).start('继续实现')
    assert result['status'] == 'partial'
    assert store.record.run_runtime['verification_required']
    assert manager.active_task()['id'] == initial['id']
    assert manager.active_task()['status'] != 'completed'
    assert list(store.record.calls) == ['actual_test']  # No automatic rerun without a change.
    runtime.close()


def test_task_state_failure_stops_before_model_or_tool_execution(tmp_path, monkeypatch):
    from genesisai.agent.loop import Runner
    from test_acceptance import FakeModel
    _, manager, initial, store, runtime = development_env(tmp_path, planned=False)
    def fail(*args, **kwargs):
        raise ToolError('task_revision_conflict', '任务版本已被另一执行器更新')
    monkeypatch.setattr(runtime.tasks, 'begin_task', fail)
    model = FakeModel()
    result = Runner(model, runtime).start('继续实现')
    assert result['status'] == 'failed'
    assert store.record.run_runtime['stop_reason'] == 'task_state_error'
    assert not model.contexts and not store.record.calls
    assert manager.active_task()['id'] == initial['id']
    runtime.close()


def test_read_only_discussion_can_read_and_update_plan_without_file_permission(tmp_path):
    from genesisai.agent.loop import Runner
    from genesisai.shared.messages import Response
    from test_acceptance import FakeModel
    from test_development_completion_gate import tool_response
    _, manager, _, store, runtime = development_env(tmp_path)
    model = FakeModel(
        tool_response('plan', {'operation': 'read'}, 'read_plan'),
        tool_response('plan', {'operation': 'update', 'expected_revision': 1,
                              'upsert': [{'id': 'extra', 'kind': 'step', 'text': '待讨论的新步骤'}]}, 'adjust_plan'),
        Response(content='讨论计划已记录。'),
    )
    assert Runner(model, runtime).start('先讨论，不要修改代码')['status'] == 'completed'
    assert all(store.record.calls[key]['result']['ok'] for key in ('read_plan', 'adjust_plan'))
    assert manager.active_task()['plan_revision'] == 2
    denied = runtime.execute({'id': 'forbidden', 'name': 'editor', 'arguments': json.dumps(
        {'operation': 'create', 'path': 'forbidden.py', 'content': 'x=1\n'})})
    assert denied['error']['code'] == 'task_constraint'
    assert not (store.workspace / 'forbidden.py').exists()
    runtime.close()


def test_phases_resume_same_goal_after_real_test_and_session_reopen(tmp_path):
    from genesisai.agent.loop import Runner
    from genesisai.app.evals.plan_steps import complete_plan
    from genesisai.core.tools.tool_runtime import ToolRuntime
    from genesisai.shared.messages import Response
    from genesisai.shared.security import Access
    from test_acceptance import FakeModel
    from test_development_completion_gate import tool_response

    workspace, manager, _, store, runtime = development_env(tmp_path, planned=False)
    initial = manager.begin_task('实现加法并测试，随后交付使用文档', new_task=True)
    manager.update_plan(expected_revision=0, upsert=[
        {'id': 'implementation', 'kind': 'phase', 'text': '实现'},
        {'id': 'code', 'parent': 'implementation', 'kind': 'step', 'text': '编写加法'},
        {'id': 'delivery', 'kind': 'phase', 'text': '交付'},
        {'id': 'docs', 'parent': 'delivery', 'kind': 'step', 'text': '编写说明'},
        {'id': 'accept', 'parent': 'delivery', 'kind': 'acceptance', 'text': '实际测试通过', 'requires_verification': True},
    ])
    first = FakeModel(
        tool_response('editor', {'operation': 'create', 'path': 'calc.py', 'content': 'def add(a, b): return a + b\n'}, 'code'),
        tool_response('editor', {'operation': 'create', 'path': 'test_calc.py',
                                'content': 'def test_add():\n    from calc import add\n    assert add(-2, 3) == 1\n'}, 'tests'),
        tool_response('run_commands', {}, 'first_verify'),
        tool_response('plan', {'operation': 'update', 'expected_revision': 1,
                              'upsert': [{'id': 'code', 'status': 'completed', 'evidence_calls': ['code']}]}, 'first_done'),
        Response(content='[[PARTIAL]] 实现已通过实际检查，使用文档尚未完成。'),
    )
    assert Runner(first, runtime).start('继续实现')['status'] == 'partial'
    midway = manager.active_task()
    assert midway['id'] == initial['id'] and midway['status'] != 'completed'
    assert next(node for node in midway['nodes'] if node['id'] == 'implementation')['status'] == 'completed'
    runtime.close()

    restored = Session(store.root, store.id, workspace=workspace)
    runtime = ToolRuntime(restored, Access([workspace], workspace, restored.root, workspace_root=workspace), permission_mode='auto', providers=[])
    second = FakeModel(
        tool_response('editor', {'operation': 'create', 'path': 'README.md', 'content': 'Use calc.add(a, b) to add numbers.\n'}, 'docs'),
        tool_response('run_commands', {}, 'verify'),
        complete_plan(runtime), Response(content='实现、文档和实际测试均完成。'),
    )
    assert Runner(second, runtime).start('继续实现')['status'] == 'completed'
    finished = runtime.tasks.active_task()
    assert finished['id'] == initial['id'] and finished['status'] == 'completed'
    assert finished['verification_passed']
    assert len(finished['run_ids']) == 2
    assert all(node['status'] == 'completed' for node in finished['nodes'])
    assert (workspace / 'README.md').read_text(encoding='utf-8').startswith('Use calc.add')
    runtime.close()
