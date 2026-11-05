"""完整日志的可用性验收：从真实调用入口验证落盘内容，不调用外部服务。"""

from genesisai.shared.paths import application_home


import json
from io import StringIO
import sys
import time

import httpx
import pytest
from openai import OpenAI
from rich.console import Console

from genesisai.agent.loop import Runner
from genesisai.core.state.logger import RunLogger
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.model.providers.deepseek import DeepSeekClient
from genesisai.shared.messages import Response
from genesisai.shared.security import Access
from genesisai.shared.run_control import RunControl
from test_acceptance import FakeModel, call, response


@pytest.fixture
def runtime_factory(tmp_path):
    def make(name='workspace', **options):
        ws = tmp_path / name
        ws.mkdir()
        store = Session(ws / '.genesis', workspace=ws)
        store.record.apply(grants=[str(ws)], output=str(ws), deadline=time.time() + 300)
        store.commit()
        runtime = ToolRuntime(store, Access([ws], ws, store.root, workspace_root=ws), providers=[], **options)
        return ws, runtime
    return make


def events(runtime, event=None):
    records = [json.loads(line) for line in runtime.logger.journal.path.read_text(encoding='utf-8').splitlines()]
    assert all(r['session_id'] == runtime.store.id and r['event_id'] and r['timestamp'] for r in records)
    return [r for r in records if event is None or r['event'] == event]


def test_full_long_input_model_response_tool_io_and_definitions(runtime_factory):
    ws, rt = runtime_factory()
    text = '正文\n' * 1000 + 'TAIL_TOOL'
    (ws/'note.md').write_bytes(text.encode('utf-8'))
    answer = '回答\n' * 1000 + 'TAIL_ANSWER'
    model = FakeModel(response(call('read_files', {'path':'note.md'}, 'read')), Response(content=answer, reasoning='reasoning-tail'))
    user = 'hello ' + 'u'*1000 + 'TAIL_INPUT'
    result = Runner(model, rt).start(user)
    assert result['status'] == 'completed'
    assert events(rt, 'user_input')[0]['payload']['text'] == user
    request = events(rt, 'model_input')[0]['payload']
    assert request['messages'][0]['content'] == model.contexts[0][0].content
    assert request['tools'] == model.tool_definitions[0]
    assert events(rt, 'model_output')[-1]['payload']['response']['content'] == answer
    assert events(rt, 'model_output')[-1]['payload']['response']['reasoning'] == 'reasoning-tail'
    execution = events(rt, 'tool_execution')
    assert json.loads(execution[0]['payload']['arguments']) == {'path':'note.md'}
    assert execution[1]['payload']['result']['data']['files'][0]['text'] == text
    assert events(rt, 'run_summary')[-1]['payload']['answer'] == answer
    assert len([e for e in execution if e['payload']['phase'] == 'end']) == 1
    assert list(rt.logger.journal.path.parent.iterdir()) == [rt.logger.journal.path]


@pytest.mark.parametrize('approve', [True, False])
def test_confirmed_and_rejected_tools_are_logged(runtime_factory, approve):
    ws, rt = runtime_factory()
    runner = Runner(FakeModel(response(call('editor', {'operation':'create', 'path':'out.md', 'content':'x'*2000}, 'write')), Response(content='done')), rt)
    assert runner.start('hello')['status'] == 'awaiting_confirmation'
    first_end = events(rt, 'tool_execution')[-1]['payload']
    assert first_end['result']['pending'] is True
    assert first_end['outcome'] == '待确认'
    runner.confirm(approve)
    assert events(rt, 'confirmation_decision')[-1]['payload']['approved'] is approve
    if approve:
        execution = events(rt, 'tool_execution')
        assert len(execution) == 4
        assert execution[-1]['payload']['approved'] is True
        assert execution[-1]['payload']['result']['ok'] is True
        assert execution[0]['payload']['invocation_id'] != execution[-1]['payload']['invocation_id']
    else:
        assert events(rt, 'tool_rejected')[-1]['payload']['result']['error']['code'] == 'confirmation_rejected'
        assert not (ws/'out.md').exists()


def test_cached_call_is_distinguishable_and_full(runtime_factory):
    _, rt = runtime_factory()
    tool_call = call('list_files', {'path':'.'})
    first = rt.execute(tool_call)
    assert rt.execute(tool_call) == first
    last = events(rt, 'tool_execution')[-1]['payload']
    assert last['prior_state'] == 'succeeded'
    assert last['result'] == first


def test_tool_failure_and_cancelled_pending_results(runtime_factory):
    _, rt = runtime_factory()
    failed = rt.execute(call('read_files', {'path':'missing.md'}, 'missing'))
    assert failed['ok'] is False
    assert events(rt, 'tool_execution')[-1]['payload']['result'] == failed
    assert events(rt, 'tool_error')[0]['payload']['error']['type'] == 'FileNotFoundError'
    runner = Runner(FakeModel(response(call('editor', {'operation':'create','path':'x.md','content':'x'}, 'write'))), rt)
    assert runner.start('hello')['status'] == 'awaiting_confirmation'
    assert runner.cancel()['status'] == 'cancelled'
    delivered = events(rt, 'tool_result_delivery')[-1]['payload']
    assert delivered['call']['id'] == 'write'
    assert delivered['result']['error']['code'] == 'user_cancelled'


def test_confirm_after_restart_keeps_same_journal(runtime_factory):
    ws, rt = runtime_factory()
    runner = Runner(FakeModel(response(call('editor', {'operation':'create','path':'x.md','content':'x'}, 'write'))), rt)
    assert runner.start('hello')['status'] == 'awaiting_confirmation'
    before = len(events(rt))
    store = Session(rt.store.root, rt.store.id, workspace=ws)
    restored = ToolRuntime(store, Access([ws], ws, store.root, workspace_root=ws), providers=[])
    result = Runner(FakeModel(Response(content='done')), restored).confirm(True)
    assert result['status'] == 'completed'
    assert len(events(restored)) > before
    assert events(restored, 'tool_execution')[-1]['payload']['prior_state'] == 'prepared'
    assert events(restored, 'tool_execution')[-1]['payload']['result']['ok'] is True


def test_retry_records_every_attempt_and_failure(runtime_factory):
    _, rt = runtime_factory()
    class APIConnectionError(Exception):
        pass
    runner = Runner(FakeModel(APIConnectionError('offline fixture'), Response(content='ok')), rt)
    assert runner.start('hello')['status'] == 'completed'
    inputs = events(rt, 'model_input')
    assert [r['payload']['attempt'] for r in inputs] == [1, 2]
    assert inputs[0]['payload']['model_call_id'] == inputs[1]['payload']['model_call_id']
    assert events(rt, 'model_error')[0]['payload']['error']['message'] == 'offline fixture'


def test_interrupted_stream_preserves_partial_content(runtime_factory):
    _, rt = runtime_factory()
    class Model:
        def stream_chat(self, *args, **kwargs):
            yield ('reasoning', 'synthetic reasoning')
            yield 'already emitted\n'
            raise RuntimeError('stream broke')
    result = Runner(Model(), rt)._call_model(rt.context_budgeter.build('direct_answer'), [])
    assert result['status'] == 'failed'
    chunks = events(rt, 'model_stream_chunk')
    assert [r['payload']['text'] for r in chunks] == ['synthetic reasoning', 'already emitted\n']
    assert events(rt, 'model_error')[0]['payload']['error']['message'] == 'stream broke'


def test_sessions_are_isolated_and_resume_appends(runtime_factory, tmp_path):
    _, first = runtime_factory('first')
    _, second = runtime_factory('second')
    first.logger.record('marker', text='FIRST_SESSION_ONLY')
    second.logger.record('marker', text='SECOND_SESSION_ONLY')
    assert 'SECOND_SESSION_ONLY' not in first.logger.journal.path.read_text()
    assert 'FIRST_SESSION_ONLY' not in second.logger.journal.path.read_text()
    restored = Session(first.store.root, first.store.id, workspace=first.store.workspace)
    logger = RunLogger(restored)
    logger.record('marker', text='RESUMED')
    assert [r['payload']['text'] for r in events(first)] == ['FIRST_SESSION_ONLY', 'RESUMED']


def test_credentials_redacted_without_truncating_regular_content(runtime_factory):
    _, rt = runtime_factory()
    rt.logger.add_secret('sk-fixture-known-secret')
    rt.logger.record('payload', body={'api_key':'other credential', 'properties':{'api_key':{'type':'string'}}},
                     text='x'*5000+'TAIL sk-fixture-known-secret',
                     arguments='{"password":"spaces inside password"}', authorization='Bearer abc.def')
    raw = rt.logger.journal.path.read_text()
    for value in ('other credential', 'sk-fixture-known-secret', 'spaces inside password', 'abc.def'):
        assert value not in raw
    assert 'x'*5000+'TAIL' in raw
    assert events(rt)[0]['payload']['body']['properties']['api_key'] == {'type':'string'}


def test_provider_request_and_raw_response_with_defaults(runtime_factory):
    _, rt = runtime_factory()
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={
            'id':'completion-fixture', 'object':'chat.completion', 'created':1, 'model':'fixture',
            'choices':[{'index':0, 'finish_reason':'stop', 'message':{'role':'assistant', 'content':'ok', 'reasoning_content':'synthetic'}}],
            'usage':{'prompt_tokens':2,'completion_tokens':3,'total_tokens':5}})
    model = DeepSeekClient('fixture', api_key='sk-fixture-private', generation={'max_tokens':123})
    model.client.close()
    model.client = OpenAI(api_key='sk-fixture-private', base_url='https://fixture.invalid',
                          http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)
    try:
        model.bind_logging(rt.logger)
        model.chat(rt.context_budgeter.build('direct_answer'), tools=rt.definitions())
        req = events(rt, 'provider_request')[0]['payload']['request']
        assert req['messages'] == captured[0]['messages']
        assert req['tools'] == captured[0]['tools']
        assert req['max_tokens'] == 123
        assert req['extra_body']['thinking'] == captured[0]['thinking']
        assert events(rt, 'provider_response')[0]['payload']['response']['id'] == 'completion-fixture'
        assert 'sk-fixture-private' not in rt.logger.journal.path.read_text(encoding='utf-8')
    finally:
        model.client.close()


def test_provider_stream_raw_chunks_and_complete_answer(runtime_factory):
    _, rt = runtime_factory()
    chunks = [
        {'id':'stream-fixture','object':'chat.completion.chunk','created':1,'model':'fixture',
         'choices':[{'index':0,'finish_reason':None,'delta':{'content':'first\n'}}]},
        {'id':'stream-fixture','object':'chat.completion.chunk','created':1,'model':'fixture',
         'choices':[{'index':0,'finish_reason':'stop','delta':{'content':'last'}}]},
    ]
    sse = ''.join('data: '+json.dumps(chunk)+'\n\n' for chunk in chunks)+'data: [DONE]\n\n'
    def handler(request):
        return httpx.Response(200, headers={'Content-Type':'text/event-stream'}, content=sse)
    model = DeepSeekClient('fixture', api_key='sk-stream-fixture')
    model.client.close()
    model.client = OpenAI(api_key='sk-stream-fixture', base_url='https://fixture.invalid',
                          http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)
    try:
        result = Runner(model, rt)._call_model(rt.context_budgeter.build('direct_answer'), [])
        assert result.content == 'first\nlast'
        assert len(events(rt, 'provider_stream_chunk')) == 2
        assert events(rt, 'provider_request')[0]['payload']['request']['stream'] is True
        assert events(rt, 'model_output')[0]['payload']['response']['content'] == 'first\nlast'
    finally:
        model.client.close()


def test_shell_full_output_exceeds_tool_truncation(runtime_factory):
    _, rt = runtime_factory(permission_mode="auto")
    result = rt.execute(call('run_commands', {'command':[sys.executable,'-c',"print('x'*13000+'TAIL_STDOUT')"]}))
    assert result['data']['truncated'] is True
    assert 'TAIL_STDOUT' not in result['data']['stdout']
    assert events(rt, 'command_output')[0]['payload']['stdout'].endswith('TAIL_STDOUT\n')


@pytest.mark.parametrize('ask', [False, True])
def test_cancel_started_command_reports_side_effect_once(runtime_factory, ask):
    ws, rt = runtime_factory(permission_mode="user" if ask else "auto")
    script = "from pathlib import Path; import time; Path('marker').write_text('ran'); print('ready',flush=True); time.sleep(5)"
    cmd = call('run_commands', {'command': [sys.executable, '-c', script]})
    runner = Runner(FakeModel(response(cmd)), rt)
    control = RunControl(lambda kind, _: control.cancel() if kind == 'command_output' else None)
    if ask:
        assert runner.start('run test')['status'] == 'awaiting_confirmation'
        result = control.execute(lambda: runner.confirm(True))
    else:
        result = control.execute(lambda: runner.start('run test'))
    assert result['status'] == 'cancelled' and (ws / 'marker').read_text() == 'ran'
    entry = rt.store.record.calls[cmd['id']]
    assert entry['executed'] and entry['state'] == 'failed'
    delivered, = [json.loads(m['content']) for m in rt.store.record.messages if m['role'] == 'tool']
    assert delivered['error']['code'] == 'command_cancelled'
    assert '可能已产生部分修改' in delivered['error']['message']
    assert not rt.store.record.pending
    Runner.check_available(rt.store.record)


def test_fast_command_bounds_display_and_preserves_full_log(runtime_factory):
    from pathlib import Path
    from genesisai.app.run_queue import RunQueue

    _, rt = runtime_factory(permission_mode="auto")
    runs = RunQueue()
    script = "import sys; print('x\\n' * 20000,end=''); print('stderr tail',file=sys.stderr)"
    try:
        runs.start(lambda: rt.execute(call('run_commands', {'command': [sys.executable, '-c', script]})))
        data = runs.future.result(timeout=10)['data']
        assert runs.events.qsize() == 1
        display = []
        runs.drain(lambda kind, item: display.append(item['text']))
        assert len(display) == 1 and len(display[0]) < 4200 and '省略' in display[0]
        chunks = [r['payload'] for r in events(rt, 'command_output_chunk')]
        assert ''.join(c['text'] for c in chunks if c['stream'] == 'stdout') == 'x\n' * 20000
        assert ''.join(c['text'] for c in chunks if c['stream'] == 'stderr') == 'stderr tail\n'
        assert len(chunks) < 10
        assert Path(data['log_path']).read_text(encoding='utf8').count('x\n') == 20000
        assert data['truncated'] and len(data['stdout']) == 12000
    finally:
        runs.close()


def test_cli_inputs_outputs_logs_command_and_new_session(tmp_path, monkeypatch):
    from genesisai.app import cli
    monkeypatch.setattr(cli, 'Console', lambda **kw: Console(file=StringIO(), width=120, force_terminal=False))
    monkeypatch.setattr(cli, 'build_model', lambda *a, **kw: (FakeModel(), False))
    commands = iter(['first', '/logs', '/help', '/new', 'second', '/logs', '/exit'])
    monkeypatch.setattr('rich.console.Console.input', lambda *a, **kw: next(commands))
    ws = tmp_path/'cli'
    assert cli.main(['--workspace',str(ws)]) == 0
    files = list((application_home()/'logs').glob('session_*.jsonl'))
    assert len(files) == 2
    records = [[json.loads(line) for line in p.read_text(encoding='utf-8').splitlines()] for p in files]
    commands_by_session = [[r['payload']['text'] for r in rows if r['event']=='cli_input'] for rows in records]
    assert sorted(commands_by_session) == sorted([['first','/logs','/help','/new'],['second','/logs','/exit']])
    assert all(any(r['event']=='cli_output' and '.jsonl' in r['payload']['text'].replace('\n','') for r in rows) for rows in records)


def test_cli_startup_failure_is_logged(tmp_path, monkeypatch):
    from genesisai.app import cli
    monkeypatch.setattr(cli, 'Console', lambda **kw: Console(file=StringIO(), force_terminal=False))
    def broken(*args, **kwargs):
        raise RuntimeError('fixture startup failure')
    monkeypatch.setattr(cli, 'build_model', broken)
    ws = tmp_path/'startup'
    assert cli.main(['--workspace',str(ws),'--prompt','hello']) == 2
    path = next((application_home()/'logs').glob('session_*.jsonl'))
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    assert any(r['event']=='cli_error' and r['payload']['error']['message']=='fixture startup failure' for r in rows)
    assert any(r['event']=='cli_output' and 'fixture startup failure' in r['payload']['text'] for r in rows)
