import json

import pytest

from genesisai.agent.loop import Runner
from genesisai.core.memory.service import MemoryService
from genesisai.core.persistence.session import Session
from genesisai.core.prompt.compaction import fingerprint
from genesisai.core.prompt.composer import PromptComposer
from genesisai.core.prompt.context_budgeter import ContextBudgeter
from genesisai.shared.messages import Response, ToolCall
from test_acceptance import FakeModel
from test_development_continuation import development_env


def test_oversized_user_input_is_accepted_readable_to_tail_and_persisted(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path, planned=False)
    original = '请读取本段资料最后的标记。\n' + '资料正文\n' * 20000 + '\n最终标记 TAIL_UNIQUE'
    captured = {}
    class Model:
        context_config = {'max_input_tokens': 16000, 'strategy': 'basic'}
        calls = 0
        def stream_chat(self, messages, tools=None, **options):
            self.calls += 1
            if self.calls == 1:
                message = next(m for m in reversed(messages) if m.role == 'user')
                view = json.loads(message.content)
                captured.update(view)
                assert 'memory_read' in {tool['function']['name'] for tool in tools}
                assert view['total_chars'] == len(original) and view['omitted_range'][1] > view['omitted_range'][0]
                yield Response(tool_calls=[ToolCall('input_tail', 'memory_read', json.dumps({
                    'ref': view['read_more']['ref'], 'view': 'content', 'offset': len(original) - 25, 'limit': 25}))])
            else:
                result = json.loads(next(m.content for m in reversed(messages) if m.role == 'tool'))
                assert 'TAIL_UNIQUE' in result['data']['text'] and result['data']['next_offset'] is None
                yield Response(content='末尾标记是 TAIL_UNIQUE。')
    try:
        result = Runner(Model(), runtime).start(original)
        assert result['status'] == 'completed', result
        assert store.record.messages[0]['content'] == original
        reopened = Session(store.root, store.id)
        memory = MemoryService(reopened)
        chunks, offset = [], 0
        while True:
            page = memory.read(captured['read_more']['ref'], view='content', offset=offset, limit=7000)
            chunks.append(page['text'])
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
        assert ''.join(chunks) == original
    finally:
        runtime.close()


def test_larger_model_restores_original_input_from_checkpoint(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path, planned=False)
    text = 'user content\n' * 10000
    store.record.messages = [{'role': 'user', 'content': text}]
    small = ContextBudgeter(store, PromptComposer(), config={'max_input_tokens': 12000, 'strategy': 'basic'})
    projected = small.prepare(small.build('direct_answer'), [])
    message = next(m for m in reversed(projected) if m.role == 'user')
    assert message.metadata['input_projection']['total_chars'] == len(text)
    store.record.context_checkpoint = {'source_count': 1, 'source_hash': fingerprint(store.record.messages),
        'messages': [{'role': 'user', 'content': message.content, 'metadata': message.metadata}]}
    store.commit()
    restored = Session(store.root, store.id)
    big = ContextBudgeter(restored, PromptComposer(), config={'max_input_tokens': 100000})
    result = big.prepare(big.build('direct_answer'), [])
    assert next(m.content for m in reversed(result) if m.role == 'user') == text
    runtime.close()


def test_long_goal_does_not_overflow_short_followup_or_plan_metadata(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path, planned=False)
    objective = '完整开发目标\n' * 18000
    task = runtime.tasks.begin_task(objective, run_id='before', execution_requested=False, new_task=True)
    runtime.tasks.finish('partial', 'still pending')
    model = FakeModel(Response(content='当前任务目标保存在计划中。'))
    model.context_config = {'max_input_tokens': 16000, 'strategy': 'basic'}
    try:
        result = Runner(model, runtime).start('继续说明当前状态')
        assert result['status'] == 'completed', result
        assert runtime.tasks.active_task()['objective'] == objective
        context = next(m.content for m in model.contexts[0] if (m.metadata or {}).get('context_source') == 'task')
        data = json.loads(context.split('\n', 1)[1])
        assert data['objective']['read_more']['section'] == 'objective'
        assert runtime.tasks.read(section='objective', offset=len(objective)-7)['text'] == objective[-7:]
        metadata = runtime.tasks.read(section='plan')
        assert 'objective' not in metadata and metadata['objective_chars'] == len(objective)
    finally:
        runtime.close()


def test_content_view_cannot_read_another_workspace_or_non_user_event(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path, planned=False)
    store.record.messages = [{'role': 'assistant', 'content': 'not a user instruction'}]
    store.commit()
    ref = store.history_projection()[0]['metadata']['history_ref']
    with pytest.raises(ValueError, match='用户消息'):
        runtime.memory.read(ref, view='content')
    other_workspace = tmp_path / 'other'
    other_workspace.mkdir()
    other = Session(store.root, workspace=other_workspace)
    with pytest.raises(ValueError, match='当前工作区'):
        MemoryService(other).read(ref)
    runtime.close()


def test_quote_heavy_input_and_repeated_overflow_keep_shrinking_only_the_view(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path, planned=False)
    original = '"\\\n' * 50000 + 'KEEP_TAIL'
    store.record.messages = [{'role': 'user', 'content': original}]
    budgeter = ContextBudgeter(store, PromptComposer(), config={'max_input_tokens': 16000, 'strategy': 'basic'})
    try:
        messages = budgeter.prepare(budgeter.build('direct_answer'), [])
        previous = budgeter.request_tokens(messages, [])
        for _ in range(2):
            messages = budgeter.prepare(messages, [], mode='overflow_recovery')
            size = budgeter.request_tokens(messages, [])
            assert size < previous
            previous = size
        assert store.record.messages[0]['content'] == original
        assert 'KEEP_TAIL' in messages[-1].content
    finally:
        runtime.close()
