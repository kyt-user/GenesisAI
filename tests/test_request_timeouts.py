import asyncio
import json
import time

import httpx
import pytest

from genesisai.model.providers.qwen import QwenClient
from genesisai.shared.request_timeouts import RequestClock, RequestTimeout, RequestTimeouts
from model_transport import mock_model_http


@pytest.mark.parametrize('value', [{'connect': 0}, {'first_token': None}, {'idle': True},
                                  {'total': -1}, {'total': float('nan')}, {'idle': float('inf')},
                                  {'timeout': 5}, []])
def test_invalid_request_limits(value):
    with pytest.raises(ValueError):
        RequestTimeouts.parse(value)


def test_clocks_separate_first_output_idle_and_total(monkeypatch):
    now = [0.0]
    monkeypatch.setattr('genesisai.shared.request_timeouts.time.monotonic', lambda: now[0])
    clock = RequestClock(RequestTimeouts(first_token=10, idle=3, total=20))
    now[0] = 9
    clock.activity()
    assert clock.pending() == ('idle', 3, 3)
    for now[0] in (11, 13, 15, 17, 19):
        clock.activity()
    now[0] = 20
    with pytest.raises(RequestTimeout) as error:
        clock.check()
    assert error.value.phase == 'total'
    assert RequestTimeouts().limited(9).total == 9
    assert RequestTimeouts(total=5).limited(9).total == 5
    assert RequestTimeouts().limited(float('inf')).total is None


def event(delta, finish=None):
    data = {'id': 'reply', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'fixture',
            'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
    return ('data: ' + json.dumps(data) + '\n\n').encode()


@pytest.mark.parametrize('phase', ['first_token', 'idle', 'total'])
def test_native_stream_deadline_ignores_heartbeats_and_closes_response(monkeypatch, phase):
    closed = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if phase == 'idle':
                yield event({'content': 'started'})
            while True:
                await asyncio.sleep(.02)
                # A live stream still obeys an explicit total deadline. Empty
                # role deltas and SSE comments never count as model progress.
                yield event({'content': 'x'} if phase == 'total' else {'role': 'assistant'})
                yield b': heartbeat\n\n'
        async def aclose(self):
            closed.append(True)
    mock_model_http(monkeypatch, lambda request: httpx.Response(200, stream=Stream(),
                    headers={'content-type': 'text/event-stream'}))
    # The first SDK request lazily imports its response types. Leave room for
    # that local setup so this test exercises an open stream, not pre-HTTP work.
    limits = {phase: .8 if phase == 'first_token' else .3}
    model = QwenClient('fixture', api_key='fixture', request_timeouts=limits)
    started = time.monotonic()
    with pytest.raises(RequestTimeout) as error:
        list(model.stream_chat([]))
    assert error.value.phase == phase
    assert .25 <= time.monotonic() - started < 2
    assert closed == [True]


@pytest.mark.parametrize('stream', [False, True])
def test_wait_for_headers_is_physically_interrupted_without_run_control(monkeypatch, stream):
    cancelled = []
    async def handle(request):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
    mock_model_http(monkeypatch, handle)
    model = QwenClient('fixture', api_key='fixture', request_timeouts={'first_token': .3})
    with pytest.raises(RequestTimeout) as error:
        list(model.stream_chat([])) if stream else model.chat([])
    assert error.value.phase == 'first_token'
    assert cancelled == [True]


def test_tool_argument_progress_can_outlive_first_output_timeout(tmp_path, monkeypatch):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event({'tool_calls': [{'index': 0, 'id': 'tool', 'type': 'function',
                         'function': {'name': 'example', 'arguments': '{"value":"'}}]})
            for _ in range(10):
                await asyncio.sleep(.04)
                yield event({'tool_calls': [{'index': 0, 'function': {'arguments': 'x'}}]})
            yield event({'tool_calls': [{'index': 0, 'function': {'arguments': '"}'}}]}, 'tool_calls')
            yield b'data: [DONE]\n\n'
    mock_model_http(monkeypatch, lambda request: httpx.Response(200, stream=Stream(),
                    headers={'content-type': 'text/event-stream'}))
    model = QwenClient('fixture', api_key='fixture', request_timeouts={'first_token': .3, 'idle': .3})
    values = list(model.stream_chat([]))
    assert values[:-1] == [('tool_call', '')] * 12
    assert json.loads(values[-1].tool_calls[0].arguments) == {'value': 'x' * 10}
    # Runner has its own budget guard; tool activity must reach it even though
    # arguments are withheld from execution until the entire response completes.
    from genesisai.agent.loop import Runner
    from test_development_continuation import development_env
    _, _, _, _, runtime = development_env(tmp_path)
    emitted = []
    try:
        runner = Runner(model, runtime, on_event=lambda kind, data: emitted.append(kind))
        result = runner._call_model(runtime.context_budgeter.build('direct_answer'), [])
        assert json.loads(result.tool_calls[0].arguments) == {'value': 'x' * 10}
        assert 'reasoning_token' not in emitted
    finally:
        runtime.close()


def test_connection_timeout_retains_its_phase_and_http_limits(monkeypatch):
    def handle(request):
        assert request.extensions['timeout'] == {'connect': 7, 'pool': 7, 'write': 11, 'read': None}
        raise httpx.ConnectTimeout('connection did not complete', request=request)
    mock_model_http(monkeypatch, handle)
    model = QwenClient('fixture', api_key='fixture', request_timeouts={'connect': 7, 'idle': 11})
    with pytest.raises(RequestTimeout) as error:
        model.chat([])
    assert (error.value.phase, error.value.seconds) == ('connect', 7)


def test_settings_worker_and_model_switch_preserve_explicit_overrides(tmp_path, monkeypatch):
    from genesisai.agent.loop import Runner
    from genesisai.core.jobs.operations import snapshot_model, restore_model
    from genesisai.model.config import build_model, load_model_config
    from genesisai.model.settings import ProviderSettings, save_settings, load_settings
    from test_development_continuation import development_env
    monkeypatch.setenv('QWEN_TOKEN_PLAN', 'offline-fixture')
    settings = ProviderSettings(provider='qwen_token_plan', model='fixture',
                                request_timeouts={'first_token': 42, 'idle': 13, 'total': 77})
    save_settings(settings, tmp_path)
    model, _ = build_model(settings=load_settings(tmp_path))
    restored = restore_model(snapshot_model(model))
    assert restored.request_timeouts == model.request_timeouts
    yaml = tmp_path / 'model.yaml'
    yaml.write_text('provider: qwen\nmodel: fixture\nrequest_timeouts:\n  first_token: 42\n  total: null\n')
    assert load_model_config(yaml).request_timeouts == {'first_token': 42, 'total': None}
    (tmp_path / 'run').mkdir()
    _, _, _, _, runtime = development_env(tmp_path / 'run')
    try:
        runner = Runner(model, runtime, request_timeouts={'idle': 9, 'total': None})
        assert runner.request_timeouts == RequestTimeouts(first_token=42, idle=9)
        restored.request_timeouts['first_token'] = 25
        runner.set_model(restored)
        assert runner.request_timeouts == RequestTimeouts(first_token=25, idle=9)
    finally:
        runtime.close()


def test_cli_only_overrides_explicit_phases_and_allows_clearing_total(tmp_path, monkeypatch):
    from genesisai.app import cli
    from genesisai.shared.messages import Response
    from test_acceptance import FakeModel
    from session_helpers import persisted_sessions
    model = FakeModel(Response(content='done'))
    model.request_timeouts = {'connect': 7, 'first_token': 45, 'idle': 90, 'total': 120}
    monkeypatch.setattr(cli, 'build_model', lambda _: (model, False))
    assert cli.main(['--workspace', str(tmp_path), '--prompt', 'hello',
                     '--request-idle-timeout', '12.5', '--request-total-timeout', 'none']) == 0
    saved, = persisted_sessions()
    assert saved['run_runtime']['request_timeouts'] == {
        'connect': 7, 'first_token': 45, 'idle': 12.5, 'total': None}


def test_removed_scalar_timeout_cannot_override_transport_limits():
    model = QwenClient('fixture', api_key='fixture')
    with pytest.raises(ValueError, match='request_timeouts'):
        model.chat([], timeout=600)


def test_empty_finish_delta_updates_runner_before_delayed_usage(tmp_path, monkeypatch):
    from genesisai.agent.loop import Runner
    from test_development_continuation import development_env
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(.2)
            yield event({}, 'stop')
            await asyncio.sleep(.3)
            yield b'data: [DONE]\n\n'
    mock_model_http(monkeypatch, lambda request: httpx.Response(200, stream=Stream(),
                    headers={'content-type': 'text/event-stream'}))
    model = QwenClient('fixture', api_key='fixture', request_timeouts={'first_token': .4, 'idle': .6})
    _, _, _, _, runtime = development_env(tmp_path)
    try:
        runner = Runner(model, runtime)
        response = runner._call_model(runtime.context_budgeter.build('direct_answer'), [])
        assert response.content == '' and response.finish_reason == 'stop'
    finally:
        runtime.close()
