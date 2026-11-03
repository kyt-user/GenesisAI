"""Request budgets, real working checkpoints, and long-run recovery."""
import copy
import json
import sys

import pytest

from genesisai.agent.loop import Runner
from genesisai.core.prompt.context_budgeter import ContextBudgeter
from genesisai.core.prompt.composer import PromptComposer
from genesisai.core.prompt.compaction import message_units, project, execution_facts
from genesisai.core.state.store import Store
from genesisai.shared.context_config import ContextConfig
from genesisai.shared.messages import Message, Response
from test_acceptance import FakeModel
from test_development_continuation import development_env
from test_development_completion_gate import tool_response


def history(count=12, size=6000):
    messages = [{'role': 'user', 'content': 'Fix calc.py, keep the API, run tests; do not delete user data.'}]
    for i in range(count):
        name = 'run_commands' if i == count - 1 else 'read_files'
        args = {'command': ['python', '-m', 'unittest']} if name == 'run_commands' else {'paths': ['calc.py']}
        data = {'stdout': 'x' * size, 'stderr': 'FAILED: add returns wrong value', 'exit_code': 1, 'passed': False} if name == 'run_commands' else {'files': [{'path': 'calc.py', 'text': 'x' * size, 'sha256': 'abc'}]}
        messages.extend([
            {'role': 'assistant', 'content': None, 'tool_calls': [{'id': f'c{i}', 'name': name, 'arguments': json.dumps(args)}]},
            {'role': 'tool', 'tool_call_id': f'c{i}', 'content': json.dumps({'ok': name != 'run_commands', 'data': data})},
        ])
    return messages


def budgeter(tmp_path, config=None):
    workspace, _, _, store, _ = development_env(tmp_path)
    store.data['messages'] = history()
    b = ContextBudgeter(store, PromptComposer(), config=config or {'max_input_tokens': 16000, 'strategy': 'basic'})
    return workspace, store, b


def test_single_long_task_compresses_without_losing_failure_and_resumes(tmp_path):
    workspace, store, b = budgeter(tmp_path)
    original = copy.deepcopy(store.data['messages'])
    tools = [{'name': 'editor', 'description': 'schema' * 100}]
    result = b.prepare(b.build('local_files'), tools)
    assert store.data['context_checkpoint']
    assert store.data['messages'] == original
    text = '\n'.join(m.content or '' for m in result)
    assert 'keep the API' in text and 'FAILED: add returns wrong value' in text
    assert '"exit_code":1' in text or '"exit_code": 1' in text
    message_units([m for m in b.working_messages()])
    assert store.data['run_runtime']['context_report']['tokens_after'] < store.data['run_runtime']['context_report']['trigger_tokens']
    restored = Store(store.root, store.id, workspace=workspace)
    restored.data['messages'].append({'role': 'user', 'content': 'Continue with the failed test.'})
    again = ContextBudgeter(restored, PromptComposer(), config={'max_input_tokens': 16000, 'strategy': 'basic'})
    projected = again.working_messages()
    assert projected[-1]['content'] == 'Continue with the failed test.'
    assert len(projected) < len(restored.data['messages'])
    saved = copy.deepcopy(restored.data['context_checkpoint'])
    again.prepare(again.build('local_files'), tools)
    assert restored.data['context_checkpoint'] == saved


def test_manual_compaction_and_invalidated_checkpoint(tmp_path):
    _, store, b = budgeter(tmp_path, {'max_input_tokens': 100000, 'context_window': 140000, 'strategy': 'basic'})
    report = b.compact(force=True)
    assert report['folded'] and report['mode'] == 'manual'
    assert len(b.working_messages()) < len(store.data['messages'])
    store.data['messages'][0]['content'] = 'Changed history'
    assert b.working_messages() == store.data['messages']
    assert store.data['context_checkpoint'] is None


def test_under_budget_keeps_full_nested_tool_results(tmp_path):
    _, store, b = budgeter(tmp_path)
    store.data['messages'] = history(1, 1000)
    before = copy.deepcopy(store.data['messages'])
    b.prepare(b.build('local_files'), [])
    assert store.data['context_checkpoint'] is None
    assert b.working_messages() == before


def test_system_tools_and_recovery_overhead_cannot_be_silently_dropped(tmp_path):
    _, store, b = budgeter(tmp_path)
    store.data['messages'] = history(1, 20)
    before = copy.deepcopy(store.data['messages'])
    with pytest.raises(ValueError, match='budget'):
        b.prepare([Message(role='system', content='S' * 100000), *b._messages(before)], [{'schema': 'X' * 100000}])
    assert store.data['messages'] == before
    assert store.data['context_checkpoint'] is None


def test_parallel_tool_pairs_and_pending_calls():
    batch = [{'role': 'assistant', 'tool_calls': [{'id': 'a'}, {'id': 'b'}]},
             {'role': 'tool', 'tool_call_id': 'b', 'content': '{}'},
             {'role': 'tool', 'tool_call_id': 'a', 'content': '{}'}]
    assert len(message_units(batch)) == 1
    with pytest.raises(ValueError):
        message_units(batch[:-1])
    with pytest.raises(ValueError):
        message_units(batch[1:])


def test_agentic_summary_and_failure_fallback_preserve_runtime_facts():
    raw = history()
    calls = []
    def summarize(data, limit):
        calls.append(data)
        return 'Next: repair the addition and rerun unittest.'
    result, report = project(raw, 8000, 2000, summarize)
    assert report['strategy'] == 'agentic' and calls
    assert 'Next: repair' in json.dumps(result)
    facts = execution_facts(result)
    assert any(f.get('result', {}).get('exit_code') == 1 for f in facts)
    def fail(*args):
        raise RuntimeError('provider unavailable')
    result, report = project(raw, 8000, 2000, fail)
    assert report['strategy'] == 'basic_fallback'
    assert 'FAILED' in json.dumps(result)


def test_large_cumulative_usage_does_not_force_partial(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    model = FakeModel(Response(content='The analysis is complete.', usage={'prompt_tokens': 17915, 'completion_tokens': 100, 'total_tokens': 200000}))
    result = Runner(model, runtime).start('Explain Python integers')
    assert result['status'] == 'completed'
    assert store.data['run_runtime']['stop_reason'] == 'model_answer'


def test_overflow_retries_once_with_smaller_context(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    store.data['messages'] = history(8, 6000)
    model = FakeModel(RuntimeError('context_length_exceeded'), Response(content='Recovered'))
    model.context_config = {'max_input_tokens': 100000, 'context_window': 140000, 'strategy': 'basic'}
    runner = Runner(model, runtime)
    response = runner._call_model(runner.context(), runtime.definitions())
    assert response.content == 'Recovered'
    assert len(model.contexts) == 2
    assert sum(len(m.content or '') for m in model.contexts[1]) < sum(len(m.content or '') for m in model.contexts[0])
    assert store.data['context_checkpoint']


def test_repeated_overflow_stops_instead_of_looping(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    store.data['messages'] = history(8, 6000)
    model = FakeModel(RuntimeError('context_length_exceeded'), RuntimeError('context_length_exceeded'))
    model.context_config = {'max_input_tokens': 100000, 'context_window': 140000, 'strategy': 'basic'}
    runner = Runner(model, runtime)
    result = runner._call_model(runner.context(), runtime.definitions())
    assert result['status'] == 'failed' and len(model.contexts) == 2
    assert store.data['run_runtime']['stop_reason'] == 'context_error'


def test_summary_call_logged_and_usage_accounted(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    model = FakeModel(Response(content='Fix add next.', usage={'prompt_tokens': 50, 'completion_tokens': 10, 'total_tokens': 60}))
    runner = Runner(model, runtime)
    assert runner._summarize_context([{'role': 'user', 'content': 'Fix add'}], 512) == 'Fix add next.'
    assert store.data['usage']['total_tokens'] == 60
    path = store.root / 'logs' / f'{store.id}.jsonl'
    events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    assert {e['event'] for e in events if e['payload'].get('purpose') == 'context_summary'} == {'model_input', 'model_output'}


def test_long_history_then_real_edit_test_and_followup_complete(tmp_path):
    workspace, _, _, store, runtime = development_env(tmp_path)
    (workspace / 'calc.py').write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
    store.data['messages'] = history(20, 6000)
    original_count = len(store.data['messages'])
    def inspect_context(messages):
        text = '\n'.join(m.content or '' for m in messages)
        assert 'keep the API' in text and 'FAILED' in text
        return tool_response('editor', {'operation': 'patch', 'path': 'calc.py',
            'patch': '@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n'}, 'fix')
    model = FakeModel(inspect_context,
        tool_response('editor', {'operation': 'create', 'path': 'test_calc.py',
            'content': 'import unittest\nfrom calc import add\nclass TestAdd(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n'}, 'testfile'),
        tool_response('run_commands', {'command': [sys.executable, '-m', 'unittest', '-v', 'test_calc'],
                                     'cwd': str(workspace)}, 'verify'),
        Response(content='修复并通过真实测试。', usage={'total_tokens': 200000}),
        Response(content='上轮已完成。'))
    model.context_config = {'max_input_tokens': 20000, 'strategy': 'basic'}
    runner = Runner(model, runtime)
    result = runner.start('修复 calc.py 并执行测试')
    assert result['status'] == 'completed'
    assert store.data['context_checkpoint']
    assert len(store.data['messages']) > original_count
    assert store.data['run_runtime']['verification_succeeded']
    assert store.data['calls']['verify']['result']['data']['exit_code'] == 0
    assert runner.start('总结上一轮结果')['status'] == 'completed'


def test_context_config_roundtrips_yaml_and_terminal_settings(tmp_path):
    from genesisai.model.config import load_model_config
    from genesisai.model.settings import ProviderSettings, save_settings, load_settings
    path = tmp_path / 'model.yaml'
    path.write_text('provider: deepseek\nmodel: fixture\ncontext:\n  max_input_tokens: 16000\n  strategy: basic\n', encoding='utf-8')
    config = load_model_config(path)
    settings = ProviderSettings(provider=config.provider, model=config.model, context=config.context)
    save_settings(settings, tmp_path / 'settings')
    assert load_settings(tmp_path / 'settings').context == config.context


def test_request_calibration_and_model_switch_reset(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    first = FakeModel()
    first.model = 'first'
    runner = Runner(first, runtime)
    b = runner.context_budgeter
    b.prepare(runner.context(), runtime.definitions())
    estimate = b._last_estimate
    b.note_usage(estimate * 2)
    assert store.data['context_estimate_factor'] == 2
    b.prepare(runner.context(), runtime.definitions())
    assert store.data['run_runtime']['context_report']['input_limit'] == b.config.input_limit // 2
    second = FakeModel()
    second.model = 'second'
    second.context_config = {'max_input_tokens': 64000, 'context_window': 100000}
    runner.set_model(second)
    assert b.config.max_input_tokens == 64000
    assert store.data['context_estimate_factor'] == 1


def test_user_constraints_are_never_silently_discarded(tmp_path):
    _, store, b = budgeter(tmp_path)
    store.data['messages'] = [{'role': 'user', 'content': 'constraint' * 10000}]
    before = copy.deepcopy(store.data['messages'])
    with pytest.raises(ValueError):
        b.prepare(b.build('local_files'), [])
    assert store.data['messages'] == before
    assert store.data['context_checkpoint'] is None


@pytest.mark.parametrize('config', [{'strategy': 'unknown'}, {'max_input_tokens': True}, {'context_window': 10}, {'unknown': 1}])
def test_invalid_capacity_config_rejected(config):
    with pytest.raises(ValueError):
        ContextConfig.parse(config)
