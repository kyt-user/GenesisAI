"""Regressions from the external coding session; synthetic data only."""

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict

import pytest

from genesisai.agent.loop import Runner
from genesisai.core.extensions.tools.shell.commands import process_invocation
from genesisai.shared.messages import Response
from test_p11_agent_runtime import FakeModel, runtime_env, tool_call


def invoke(runtime, name, args, ident):
    return runtime.execute(asdict(tool_call(name, args, ident)))


@pytest.mark.skipif(os.name != 'nt', reason='Windows batch argv')
@pytest.mark.parametrize('extension', ['.bat', '.cmd'])
def test_windows_batch_resolves_path_and_preserves_literal_arguments(tmp_path, monkeypatch, extension):
    directory = tmp_path / 'tool directory'
    directory.mkdir()
    script = directory / ('fixture' + extension)
    helper = directory / 'args.py'
    helper.write_text('import json,sys; print(json.dumps(sys.argv[1:]))')
    script.write_text(f'@echo off\n"{sys.executable}" "{helper}" %*\n')
    monkeypatch.setenv('PATH', str(directory))
    args = ['space argument', 'a&b', '%PATH%', '!TEMP!', 'a^b', '(value)', '尾部\\', 'space trail\\']
    for cwd, entry in ((tmp_path, 'fixture'), (directory, '.\\fixture')):
        command, env = process_invocation([entry, *args], cwd)
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == args


def test_missing_program_and_malformed_argv_are_not_executed(runtime_env):
    store, runtime = runtime_env
    for i, command in enumerate((['not-a-real-genesis-tool', 'doctor'], ['flutter --version', 'dart --version'])):
        result = invoke(runtime, 'run_commands', {'command': command}, f'missing{i}')
        assert result['error']['code'] == 'command_not_found'
        assert command[0] in result['error']['message']
        assert str(store.workspace) in result['error']['message']
        assert store.record.calls[f'missing{i}']['executed'] is False
    result = invoke(runtime, 'run_commands', {'command': '["python"]', 'timeout_seconds': '10'}, 'types')
    assert result['error']['code'] == 'validation_error'
    if os.name == 'nt':
        result = invoke(runtime, 'run_commands', {'command': ['echo', 'hello']}, 'builtin')
        assert 'cmd.exe' in result['error']['message']


@pytest.mark.skipif(os.name != 'nt', reason='Windows foreground/background batch')
def test_batch_approval_and_foreground_background_use_same_resolver(runtime_env):
    store, runtime = runtime_env
    script = store.workspace / 'fixture.cmd'
    script.write_text('@echo off\necho fixture-ready\n')
    runtime.set_permission('user')
    for background in (False, True):
        ident = 'bg' if background else 'fg'
        call = asdict(tool_call('run_commands', {'command': ['fixture'], 'background': background}, ident))
        preview = runtime.execute(call)
        assert preview['pending'] and preview['preview']['command'][0] == str(script)
        result = runtime.execute(call, store.record.calls[ident]['digest'])
        assert result['ok'], result
        if background:
            try:
                job = result['data']['job_id']
                deadline = time.monotonic() + 5
                while runtime.jobs.status(job)['status'] == 'running' and time.monotonic() < deadline:
                    time.sleep(.02)
                output = runtime.jobs.output(job)
                assert output['exit_code'] == 0 and 'fixture-ready' in output['text']
            finally:
                runtime.jobs.close()
        else:
            assert result['data']['exit_code'] == 0 and 'fixture-ready' in result['data']['stdout']
    bad = invoke(runtime, 'run_commands', {'command': ['fixture', 'x" & echo unsafe']}, 'bad_quote')
    assert bad['error']['code'] == 'validation_error'
    assert store.record.calls['bad_quote']['executed'] is False


def test_failure_limit_and_followup_include_actual_ledger(runtime_env):
    store, runtime = runtime_env
    model = FakeModel(*[Response(tool_calls=[tool_call('run_commands', {'command': ['missing-genesis-fixture', str(i)]}, f'bad{i}')]) for i in range(3)])
    runner = Runner(model, runtime)
    result = runner.start('运行项目测试，调用 run_commands')
    assert result['status'] == 'limit_reached', result
    assert 'missing-genesis-fixture' in result['answer'] and 'command_not_found' in result['answer']
    assert '没有取得可用证据' not in result['answer']
    runner.model = FakeModel(Response(content='入口未找到，不能据此判断整个执行通道故障。'))
    assert runner.start('怎么回事')['status'] == 'completed'
    context = '\n'.join(str(m.content) for m in runner.model.contexts[0])
    assert 'recent_execution' in context and 'executor_started' in context
    assert 'missing-genesis-fixture' in context and 'command_not_found' in context


def test_file_reference_recovery_lists_current_sources_and_rereads_stale_once(runtime_env):
    store, runtime = runtime_env
    path = store.workspace / 'notes.txt'
    path.write_text('old body', encoding='utf-8')
    old = invoke(runtime, 'read_files', {'path': str(path)}, 'read_old')['source_refs'][0]
    path.write_text('current body', encoding='utf-8')
    other = store.workspace / 'valid.txt'
    other.write_text('still current', encoding='utf-8')
    valid = invoke(runtime, 'read_files', {'path': str(other)}, 'read_valid')['source_refs'][0]
    def answer(_):
        new = store.record.calls['reread']['result']['source_refs'][0]
        return Response(content=f'当前文件已更新 [{new}]，另一文件有效 [{valid}]。')
    model = FakeModel(Response(content=f'引用旧文件 [{old}]'),
                      Response(tool_calls=[tool_call('read_files', {'paths': [str(path)]}, 'reread')]), answer)
    result = Runner(model, runtime).start('说明文件内容')
    assert result['status'] == 'completed', result
    assert old not in result['answer'] and valid in result['answer']
    assert valid in '\n'.join(str(m.content) for m in model.contexts[1])
    assert [d['function']['name'] for d in model.tools[1]] == ['read_files']
    assert model.tools[2] == []


@pytest.mark.parametrize('budget', [1, 12])
def test_truncated_invalid_citation_is_never_delivered_or_saved(runtime_env, budget):
    store, runtime = runtime_env
    bad = 'src_0000000000000000'
    model = FakeModel(Response(content=f'不可交付 [{bad}]。', finish_reason='length'),
                      Response(content=f'仍不可交付 [{bad}]。', finish_reason='length'))
    result = Runner(model, runtime, max_rounds=budget).start('解释这个概念')
    assert result['status'] == 'partial', result
    assert bad not in result['answer'] and bad not in json.dumps(store.record.messages)
    if budget > 1:
        assert '完整重写' in '\n'.join(str(m.content) for m in model.contexts[-1])


@pytest.mark.parametrize('escape', [False, True])
def test_stale_reread_is_bounded_to_paths_and_uses_normal_permissions(runtime_env, tmp_path, escape):
    store, runtime = runtime_env
    outside = tmp_path / 'external.txt'
    outside.write_text('old', encoding='utf-8')
    ref = invoke(runtime, 'read_files', {'path': str(outside)}, 'initial')['source_refs'][0]
    outside.write_text('new', encoding='utf-8')
    other = tmp_path / 'unrequested.txt'
    other.write_text('must not read', encoding='utf-8')
    def invalid(_):
        # Scope changes do not grant access; this simulates an external file
        # becoming stale while the model is answering in this run.
        runtime.set_permission('user')
        store.record.sources[ref]['stale'] = True
        return Response(content=f'old citation [{ref}]')
    runner = Runner(FakeModel(invalid,
        Response(tool_calls=[tool_call('read_files', {'paths': [str(other if escape else outside)]}, 'reread')]),
        Response(content='[[PARTIAL]] 文件尚未核实，不能引用旧内容。')), runtime)
    result = runner.start('说明内容')
    if escape:
        assert 'reread' not in store.record.calls
    else:
        denied = store.record.calls['reread']
        assert denied['result']['error']['code'] == 'permission_denied'
        # read_files checks scope inside its implementation. Entering that
        # implementation is recorded, but no file content/source is returned.
        assert denied['result']['data'] is None
        assert denied['result']['source_refs'] == []
    assert result['status'] == 'partial', result
    assert ref not in result['answer']


def test_answer_and_recovery_follow_configured_output_budget(runtime_env):
    _, runtime = runtime_env
    class Model(FakeModel):
        generation = {'max_tokens': 2048}
        options = []
        def stream_chat(self, messages, tools=None, **kwargs):
            self.options.append(kwargs)
            yield from super().stream_chat(messages, tools, **kwargs)
    model = Model(Response(content='unfinished', finish_reason='length'), Response(content='完整回答。'))
    result = Runner(model, runtime).start('解释一个概念')
    assert result['status'] == 'completed'
    assert [o['max_tokens'] for o in model.options] == [2048, 2048]


def test_local_citations_are_checked_before_stream_display(runtime_env):
    store, runtime = runtime_env
    path = store.workspace / 'facts.txt'
    path.write_text('facts', encoding='utf-8')
    invoke(runtime, 'read_files', {'path': str(path)}, 'read')
    events = []
    model = FakeModel(Response(content='invalid [src_0000000000000000]'), Response(content='可读说明。'))
    result = Runner(model, runtime, on_event=lambda event, payload: events.append(event)).start('解释文件')
    assert result['status'] == 'completed'
    assert 'stream_token' not in events


def test_terminal_trim_cannot_restart_price_recovery(runtime_env):
    store, runtime = runtime_env
    url = 'https://example.com/fixture-price'
    runtime.network.fetch = lambda value: {'url': value, 'title': 'Price', 'text': 'USD 8', 'truncated': False}
    def truncated(_):
        ref = store.record.run_runtime['evidence_refs'][0]
        return Response(content=f'Price USD 8. [{ref}] unfinished', finish_reason='length')
    model = FakeModel(Response(tool_calls=[tool_call('fetch_web_content', {'url': url}, 'read')]), truncated, truncated)
    result = Runner(model, runtime).start(url + ' price')
    assert result['status'] == 'partial', result
    assert len(model.contexts) == 3
    assert 'USD 8' not in result['answer']


def test_prior_tool_body_is_retrievable_and_current_body_is_untouched(runtime_env):
    store, runtime = runtime_env
    old = json.dumps({'ok': True, 'data': {'text': 'original body ' * 5000}, 'source_refs': ['src_123']})
    store.record.messages = [
        {'role': 'user', 'content': 'read'},
        {'role': 'assistant', 'content': None, 'tool_calls': [asdict(tool_call('read_files', {'path': 'fixture'}, 'r1'))]},
        {'role': 'tool', 'content': old, 'tool_call_id': 'r1'},
        {'role': 'assistant', 'content': 'already read'},
        {'role': 'user', 'content': 'followup'},
        {'role': 'assistant', 'content': None, 'tool_calls': [asdict(tool_call('read_files', {'path': 'fixture'}, 'r2'))]},
        {'role': 'tool', 'content': old, 'tool_call_id': 'r2'},
    ]
    store.commit()
    runner = Runner(FakeModel(), runtime)
    projected = runner.context_budgeter.working_messages()
    assert len(projected[2]['content']) < len(old) / 10
    assert projected[6]['content'] == old
    ref = json.loads(projected[2]['content'])['history_ref']
    text, offset = '', 0
    while True:
        page = runtime.memory.read(ref, offset=offset, limit=16000)
        text += page['text']
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    assert json.loads(text)['content'] == old
    assert store.record.messages[2]['content'] == old
