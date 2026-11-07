from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from threading import Timer

import pytest

from genesisai.agent.loop import Runner
from genesisai.shared.messages import Response
from genesisai.shared.retry_policy import RetryPolicy
from genesisai.shared.run_control import RunControl
from test_development_continuation import development_env


def test_retry_after_seconds_and_date_override_backoff(monkeypatch):
    policy = RetryPolicy.parse({'jitter_ratio': 0})
    error = RuntimeError('busy')
    error.response = SimpleNamespace(headers={'retry-after': '45'})
    assert policy.delay(0, error) == 45
    monkeypatch.setattr('genesisai.shared.retry_policy.time.time', lambda: 1000)
    error.response.headers['retry-after'] = format_datetime(datetime.fromtimestamp(1060, timezone.utc), usegmt=True)
    assert policy.delay(0, error) == 60
    error.response.headers['retry-after'] = 'invalid'
    assert policy.delay(2, error) == 4
    assert policy.delay(100000, error) == 30


@pytest.mark.parametrize('value', [{'budget_seconds': -1}, {'initial_delay_seconds': True},
                                 {'jitter_ratio': 2}, {'budget_seconds': float('inf')},
                                 {'max_attempts': 3}, []])
def test_invalid_retry_configuration_is_rejected(value):
    with pytest.raises(ValueError):
        RetryPolicy.parse(value)


def test_provider_recovers_after_more_than_four_identical_failures(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    class APIConnectionError(Exception):
        pass
    class Model:
        calls = 0
        retry_config = {'initial_delay_seconds': 0, 'max_delay_seconds': 0}
        def stream_chat(self, *args, **kwargs):
            self.calls += 1
            if self.calls <= 5:
                raise APIConnectionError('temporary outage')
            yield Response(content='恢复成功')
    model = Model()
    try:
        result = Runner(model, runtime).start('hello')
        assert result['status'] == 'completed'
        assert model.calls == 6
        assert store.record.run_runtime['recovery_counts']['provider'] == 5
    finally:
        runtime.close()


def test_retry_after_exceeding_budget_does_not_retry_early(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    class RateLimitError(Exception):
        response = SimpleNamespace(headers={'retry-after': '120'})
    class Model:
        calls = 0
        retry_config = {'budget_seconds': 30}
        def stream_chat(self, *args, **kwargs):
            self.calls += 1
            raise RateLimitError('limited')
    model = Model()
    try:
        assert Runner(model, runtime).start('hello')['status'] == 'failed'
        assert model.calls == 1
        assert store.record.run_runtime['stop_reason'] == 'provider_retry_budget'
    finally:
        runtime.close()


def test_cancel_interrupts_provider_backoff(tmp_path):
    _, _, _, _, runtime = development_env(tmp_path)
    class APIConnectionError(Exception):
        pass
    control = RunControl(lambda *args: None)
    timer = None
    class Model:
        retry_config = {'initial_delay_seconds': 30, 'max_delay_seconds': 30}
        def stream_chat(self, *args, **kwargs):
            nonlocal timer
            timer = Timer(.02, control.cancel)
            timer.start()
            raise APIConnectionError('temporary outage')
    try:
        assert control.execute(lambda: Runner(Model(), runtime).start('hello'))['status'] == 'cancelled'
    finally:
        if timer:
            timer.join()
        runtime.close()


def test_provider_recovery_config_survives_settings_and_worker_snapshot(tmp_path, monkeypatch):
    from genesisai.model.settings import ProviderSettings, save_settings, load_settings
    from genesisai.model.config import build_model
    from genesisai.core.jobs.operations import snapshot_model, restore_model
    monkeypatch.setenv('QWEN_TOKEN_PLAN', 'offline-fixture')
    settings = ProviderSettings(provider='qwen_token_plan', model='fixture', retry={'budget_seconds': 123})
    save_settings(settings, tmp_path)
    model, _ = build_model(settings=load_settings(tmp_path))
    restored = restore_model(snapshot_model(model))
    assert restored.provider == 'qwen_token_plan'
    assert restored.retry_config == {'budget_seconds': 123}


@pytest.mark.parametrize('controlled', [False, True])
@pytest.mark.parametrize('failure', ['disconnect', 'timeout', 'eof'])
def test_interrupted_sdk_stream_retries_without_executing_partial_tool_call(tmp_path, monkeypatch, controlled, failure):
    import json
    import httpx
    from genesisai.model.config import build_model
    from genesisai.model.settings import ProviderSettings

    monkeypatch.setenv('QWEN_TOKEN_PLAN', 'offline-fixture')
    requests, closed = [], []
    def chunks(first):
        delta = ({'tool_calls': [{'index': 0, 'id': 'never_execute', 'type': 'function',
                  'function': {'name': 'editor', 'arguments': '{'}}]} if first else {'content': 'done'})
        data = {'id': 'reply', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'fixture',
                'choices': [{'index': 0, 'delta': delta, 'finish_reason': None if first else 'stop'}]}
        yield ('data: ' + json.dumps(data) + '\n\n').encode()
        if first and failure != 'eof':
            raise (httpx.RemoteProtocolError('server disconnected') if failure == 'disconnect' else httpx.ReadTimeout('stream idle'))
    class AsyncStream(httpx.AsyncByteStream):
        def __init__(self, first): self.first = first
        async def __aiter__(self):
            for item in chunks(self.first): yield item
        async def aclose(self): closed.append(self.first)
    def handle(request):
        requests.append(request)
        stream = AsyncStream(len(requests) == 1)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)

    model, _ = build_model(settings=ProviderSettings(provider='qwen_token_plan', model='fixture',
        retry={'initial_delay_seconds': 0, 'max_delay_seconds': 0}))
    from model_transport import mock_model_http
    mock_model_http(monkeypatch, handle)
    _, _, _, store, runtime = development_env(tmp_path, planned=False)
    try:
        action = lambda: Runner(model, runtime).start('hello')
        result = RunControl(lambda *args: None).execute(action) if controlled else action()
        assert result['status'] == 'completed'
        assert len(requests) == 2 and store.record.run_runtime['recovery_counts']['provider'] == 1
        assert not store.record.calls
        assert sorted(closed) == [False, True]
    finally:
        runtime.close()
