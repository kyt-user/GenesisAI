"""Pages fit the request and retain exact cursors even with JSON escaping."""
import json

import pytest

from genesisai.core.persistence.session import Session
from genesisai.core.prompt.composer import PromptComposer
from genesisai.core.prompt.context_budgeter import ContextBudgeter
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.shared.messages import Message
from genesisai.shared.security import Access
from test_acceptance import call


@pytest.fixture
def runtime(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    store = Session(tmp_path / 'state', workspace=workspace)
    store.record.apply(grants=[str(workspace)], output=str(workspace))
    store.commit()
    runtime = ToolRuntime(store, Access([workspace], workspace, store.root, workspace_root=workspace), providers=[])
    try:
        yield runtime
    finally:
        runtime.close()


def user_source(runtime, text):
    runtime.store.record.messages.append({'role': 'user', 'content': text})
    runtime.store.commit()
    return runtime.store.history_projection()[-1]['metadata']['history_ref']


def read_tool(runtime, ref, identity, **kwargs):
    tool_call = call('memory_read', {'ref': ref, 'view': 'content', **kwargs}, identity)
    result = runtime.execute(tool_call)
    assert result['ok'], result
    wire = runtime.model_content(tool_call, result)
    visible = json.loads(wire)
    assert not visible.get('truncated'), visible
    assert visible['data'] == result['data']
    budget = runtime.context_budgeter.read_page_tokens()
    assert ContextBudgeter.request_tokens([Message('tool', wire, tool_call_id=identity)], []) <= budget
    return result['data']


def test_default_page_expands_with_capacity_and_bypasses_old_output_cap(runtime):
    original = '分页完整原文 ' * 30000 + 'EXACT_TAIL'
    ref = user_source(runtime, original)
    runtime.context_budgeter.configure({'max_input_tokens': 16000})
    small = read_tool(runtime, ref, 'small')
    assert 0 < len(small['text']) < 6000
    runtime.context_budgeter.configure({'max_input_tokens': 400000})
    large = read_tool(runtime, ref, 'large')
    assert 24000 < len(large['text']) <= 200000
    assert large['next_offset'] == len(large['text'])
    assert large['total_chars'] == len(original)
    assert large['text'] == original[:large['next_offset']]
    explicit = read_tool(runtime, ref, 'explicit', offset=len(original)-10, limit=10)
    assert explicit['text'] == original[-10:] and explicit['next_offset'] is None
    assert runtime.store.record.messages[0]['content'] == original


def test_current_occupancy_and_usage_calibration_reduce_page(runtime):
    ref = user_source(runtime, 'source data ' * 30000)
    runtime.context_budgeter.configure({'max_input_tokens': 100000})
    baseline = read_tool(runtime, ref, 'baseline')
    runtime.store.record.context_estimate_factor = 2
    calibrated = read_tool(runtime, ref, 'calibrated')
    assert len(calibrated['text']) < len(baseline['text'])
    runtime.store.record.run_runtime['context_report'] = {
        'tokens_after': runtime.context_budgeter.config.input_limit // 2 - 5000}
    occupied = read_tool(runtime, ref, 'occupied', limit=200000)
    assert 0 < len(occupied['text']) < len(calibrated['text'])
    assert occupied['next_offset'] == len(occupied['text'])


def test_escaped_pages_reconstruct_every_character_after_restore(runtime):
    original = '引号"\\换行\n\x00🙂' * 800 + 'EXACT_TAIL'
    ref = user_source(runtime, original)
    chunks, offset = [], 0
    while True:
        page = runtime.memory.read(ref, view='content', offset=offset, limit=200000, page_tokens=1024)
        assert page['text']
        assert page['text'] == original[offset:offset + len(page['text'])]
        chunks.append(page['text'])
        if page['next_offset'] is None:
            break
        assert page['next_offset'] == offset + len(page['text'])
        offset = page['next_offset']
    assert len(chunks) > 2
    assert ''.join(chunks) == original
    reopened = Session(runtime.store.root, runtime.store.id)
    from genesisai.core.memory.service import MemoryService
    assert MemoryService(reopened).read(ref, view='content', offset=len(original)-10)['text'] == original[-10:]
    empty = runtime.memory.read(ref, view='content', offset=len(original)+10, page_tokens=1024)
    assert empty['text'] == '' and empty['next_offset'] is None
    with pytest.raises(ValueError, match='分页预算'):
        runtime.memory.read(ref, page_tokens=1)


def test_quote_heavy_tool_page_fits_actual_wire_envelope(runtime):
    ref = user_source(runtime, '"\\\n\x01' * 12000)
    runtime.context_budgeter.configure({'max_input_tokens': 16000})
    page = read_tool(runtime, ref, 'escaped', limit=200000)
    assert 0 < len(page['text']) < 4000
    assert page['next_offset'] == len(page['text'])


def test_latest_page_is_delivered_even_when_larger_than_history_allowance(runtime):
    text = 'large original body\n' * 16000 + 'TAIL_VERIFIED'
    ref = user_source(runtime, text)
    store = runtime.store
    store.record.messages.extend([{'role': 'assistant', 'content': 'archived'},
                                  {'role': 'user', 'content': 'Read first page then the last 80 characters.'}])
    runtime.context_budgeter.configure({'max_input_tokens': 100000, 'history_tokens': 1000, 'strategy': 'basic'})
    b = runtime.context_budgeter
    b.prepare(b.build('direct_answer'), [])
    tool_call = call('memory_read', {'ref': ref, 'view': 'content', 'offset': 0}, 'first_page')
    result = runtime.execute(tool_call)
    assert result['ok'] and len(result['data']['text']) > 6000
    wire = runtime.model_content(tool_call, result)
    store.record.messages.extend([{'role': 'assistant', 'tool_calls': [tool_call]},
        {'role': 'tool', 'tool_call_id': tool_call['id'], 'content': wire}])
    model_request = b.prepare(b.build('direct_answer'), [])
    observation = next(m for m in reversed(model_request) if m.role == 'tool')
    assert json.loads(observation.content)['data'] == result['data']
    assert store.record.context_checkpoint
    reopened = Session(store.root, store.id)
    resumed = ContextBudgeter(reopened, PromptComposer(), config={
        'max_input_tokens': 100000, 'history_tokens': 1000, 'strategy': 'basic'})
    again = resumed.prepare(resumed.build('direct_answer'), [])
    assert json.loads(next(m.content for m in reversed(again) if m.role == 'tool'))['data'] == result['data']
