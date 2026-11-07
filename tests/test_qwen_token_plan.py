"""Token Plan 配置入口与真实 SDK 协议验证，网络由 MockTransport 接管。"""

import json
from io import StringIO

import httpx
import pytest
from rich.console import Console

from genesisai.app import onboarding
from genesisai.core.state.io_journal import IOJournal
from genesisai.core.persistence.session import Session
from genesisai.model.config import build_model
from genesisai.model.providers.catalog import get_provider, provider_ids
from genesisai.model.settings import ProviderSettings, load_settings
from genesisai.shared.messages import Message


@pytest.fixture(autouse=True)
def isolate_token_plan(monkeypatch):
    monkeypatch.delenv('QWEN_TOKEN_PLAN', raising=False)
    monkeypatch.delenv('QWEN_TOKEN_PLAN_BASE_URL', raising=False)


def test_yaml_build_uses_plan_endpoint_and_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('QWEN_TOKEN_PLAN', 'sk-sp-test-plan')
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'ordinary-key')
    monkeypatch.setenv('QWEN_BASE_URL', 'https://ordinary.example/v1')
    config = tmp_path / 'model.yaml'
    config.write_text('provider: qwen_token_plan\nmodel: qwen3.8-flash\n', encoding='utf-8')
    model, remote = build_model(config)
    assert remote is True
    assert model.api_key == 'sk-sp-test-plan'
    assert str(model.base_url).rstrip('/') == get_provider('qwen_token_plan').base_url


def test_missing_plan_key_does_not_use_dashscope(monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'ordinary-key')
    with pytest.raises(RuntimeError, match='QWEN_TOKEN_PLAN'):
        build_model(settings=ProviderSettings(provider='qwen_token_plan', model='qwen3.8-flash'))


@pytest.mark.parametrize('use_env', [True, False])
def test_terminal_selects_plan_and_persists_correct_endpoint(tmp_path, monkeypatch, use_env):
    console = Console(file=StringIO(), force_terminal=False)
    picks = iter([provider_ids().index('qwen_token_plan'), 0])
    monkeypatch.setattr(onboarding.select, 'select', lambda *args, **kwargs: next(picks))
    if use_env:
        monkeypatch.setenv('QWEN_TOKEN_PLAN', 'sk-sp-env-test')
    def prompt_secret(*args, **kwargs):
        assert not use_env, 'Environment credentials must skip the secret prompt'
        return 'sk-sp-terminal-test'
    monkeypatch.setattr(onboarding.select, 'prompt_secret', prompt_secret)
    result = onboarding.run_onboarding(console, directory=tmp_path)
    saved = load_settings(tmp_path)
    assert result == saved
    assert saved.provider == 'qwen_token_plan'
    assert saved.model == 'qwen3.8-flash'
    assert saved.base_url == get_provider(saved.provider).base_url
    assert saved.api_key == ('' if use_env else 'sk-sp-terminal-test')
    assert onboarding.effective_key(saved) == ('sk-sp-env-test' if use_env else 'sk-sp-terminal-test')


@pytest.mark.parametrize('stream', [False, True])
def test_sdk_tool_call_roundtrip(stream, monkeypatch):
    requests = []
    tool_call = {'id': 'call_read', 'type': 'function', 'function': {'name': 'read_files', 'arguments': '{"path":"main.py"}'}}
    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert str(request.url) == get_provider('qwen_token_plan').base_url + '/chat/completions'
        assert request.headers['authorization'] == 'Bearer sk-sp-test'
        assert 'thinking' not in payload and 'reasoning_effort' not in payload
        first = len(requests) == 1
        message = {'role': 'assistant', 'content': None, 'tool_calls': [tool_call]} if first else {'role': 'assistant', 'content': 'done'}
        finish = 'tool_calls' if first else 'stop'
        if payload.get('stream'):
            if first:
                message['tool_calls'] = [dict(tool_call, index=0)]
            chunk = {'id': 'chunk', 'object': 'chat.completion.chunk', 'created': 0, 'model': payload['model'], 'choices': [{'index': 0, 'delta': message, 'finish_reason': finish}]}
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text='data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={'id': 'reply', 'object': 'chat.completion', 'created': 0, 'model': payload['model'], 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}]})

    model, _ = build_model(settings=ProviderSettings(provider='qwen_token_plan', model='qwen3.8-flash', api_key='sk-sp-test'))
    from model_transport import mock_model_http
    mock_model_http(monkeypatch, handle)
    tools = [{'type': 'function', 'function': {'name': 'read_files', 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}}]
    messages = [Message(role='user', content='read main.py')]
    def invoke():
        if stream:
            return list(model.stream_chat(messages, tools=tools))[-1]
        return model.chat(messages, tools=tools)
    reply = invoke()
    assert reply.tool_calls[0].name == 'read_files'
    messages.extend([Message(role='assistant', content=reply.content, tool_calls=reply.tool_calls), Message(role='tool', content='print(1)', tool_call_id='call_read')])
    assert invoke().content == 'done'
    assert requests[1]['messages'][-1]['tool_call_id'] == 'call_read'


def test_plan_key_is_redacted_before_model_binding(tmp_path, monkeypatch):
    monkeypatch.setenv('QWEN_TOKEN_PLAN', 'sk-sp-private-env')
    journal = IOJournal(Session(tmp_path / '.genesis', workspace=tmp_path))
    assert journal.clean('echo sk-sp-private-env') == 'echo [REDACTED]'
    assert journal.clean({'QWEN_TOKEN_PLAN': 'short'}) == {'QWEN_TOKEN_PLAN': '[REDACTED]'}
    assert journal.clean('QWEN_TOKEN_PLAN=unregistered-key') == 'QWEN_TOKEN_PLAN=[REDACTED]'
