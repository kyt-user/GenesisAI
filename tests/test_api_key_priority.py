"""核验实际 SDK 凭证及启动/配置分支，使用虚构密钥，不发送 API 请求。"""

from io import StringIO

import pytest
from rich.console import Console

from genesisai.app import cli, onboarding
from genesisai.app.terminal_view import CliView
from genesisai.model.config import build_model
from genesisai.model.credentials import environment_key
from genesisai.model.settings import ProviderSettings, load_settings, save_settings
from test_acceptance import FakeModel


@pytest.fixture(autouse=True)
def isolate_credentials(monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    monkeypatch.delenv('QWEN_TOKEN_PLAN', raising=False)


@pytest.mark.parametrize('provider,variable', [('deepseek','DEEPSEEK_API_KEY'), ('qwen','DASHSCOPE_API_KEY'), ('qwen_token_plan','QWEN_TOKEN_PLAN')])
@pytest.mark.parametrize('environment,expected', [(' env-key ', 'env-key'), ('', 'terminal-key'), ('   ', 'terminal-key'), (None, 'terminal-key')])
def test_actual_client_environment_first_then_terminal(monkeypatch, provider, variable, environment, expected):
    if environment is not None:
        monkeypatch.setenv(variable, environment)
    settings = ProviderSettings(provider=provider, model='fixture', api_key='terminal-key')
    model, _ = build_model(settings=settings)
    try:
        assert model.client.api_key == expected
        assert onboarding.effective_key(settings) == expected
        assert settings.api_key == 'terminal-key'
    finally:
        model.client.close()


def test_empty_injected_environment_does_not_use_process(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'process-key')
    settings = ProviderSettings(provider='deepseek', model='fixture', api_key='terminal-key')
    assert environment_key('deepseek', {}) == ''
    assert onboarding.effective_key(settings, {}) == 'terminal-key'


@pytest.mark.parametrize('provider,variable', [('deepseek', 'DEEPSEEK_API_KEY'), ('qwen_token_plan', 'QWEN_TOKEN_PLAN')])
@pytest.mark.parametrize('process,workspace,project,expected', [
    ('process-key', 'workspace-key', 'project-key', 'process-key'),
    (None, 'workspace-key', 'project-key', 'workspace-key'),
    (None, '', 'project-key', 'project-key'),
    ('   ', 'workspace-key', 'project-key', 'workspace-key'),
    (None, '   ', '', 'terminal-key'),
])
def test_dotenv_chain_reaches_actual_client(tmp_path, monkeypatch, provider, variable, process, workspace, project, expected):
    root = tmp_path/'repo'
    work = tmp_path/'work'
    root.mkdir()
    work.mkdir()
    monkeypatch.setattr(cli, 'PROJECT_ROOT', root)
    if process is not None:
        monkeypatch.setenv(variable, process)
    (work/'.env').write_text(f'{variable}="{workspace}"\n', encoding='utf-8')
    (root/'.env').write_text(f'{variable}="{project}"\n', encoding='utf-8')
    cli.load_environment(workspace=work)
    model, _ = build_model(settings=ProviderSettings(provider=provider, model='fixture', api_key='terminal-key'))
    try:
        assert model.client.api_key == expected
    finally:
        model.client.close()


def test_explicit_env_file_replaces_auto_search(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'PROJECT_ROOT', tmp_path)
    (tmp_path/'.env').write_text('DEEPSEEK_API_KEY=ignored-auto\n', encoding='utf-8')
    explicit = tmp_path/'chosen.env'
    explicit.write_text('DEEPSEEK_API_KEY=explicit-key\n', encoding='utf-8')
    cli.load_environment(explicit, tmp_path)
    assert environment_key('deepseek') == 'explicit-key'


@pytest.mark.parametrize('saved', ['', 'terminal-key'])
def test_auth_skips_key_prompt_and_does_not_copy_environment(tmp_path, monkeypatch, saved):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'environment-only-key')
    picks = iter([0, 0])
    monkeypatch.setattr(onboarding.select, 'select', lambda *a, **kw: next(picks))
    def unexpected(*a, **kw):
        raise AssertionError('must not request terminal key when env is set')
    monkeypatch.setattr(onboarding.select, 'prompt_secret', unexpected)
    initial = ProviderSettings(provider='deepseek', model='fixture', api_key=saved)
    console = Console(file=StringIO(), force_terminal=False)
    configured = onboarding.run_onboarding(console, directory=tmp_path/'cfg', initial=initial)
    persisted = load_settings(tmp_path/'cfg')
    assert configured.api_key == persisted.api_key == saved
    assert 'environment-only-key' not in (tmp_path/'cfg/config.json').read_text()
    assert onboarding.effective_key(configured) == 'environment-only-key'


def test_missing_environment_prompts_for_terminal_key(tmp_path, monkeypatch):
    picks = iter([0, 0])
    monkeypatch.setattr(onboarding.select, 'select', lambda *a, **kw: next(picks))
    prompts = []
    def prompt(*args, **kwargs):
        prompts.append(kwargs)
        return 'entered-key'
    monkeypatch.setattr(onboarding.select, 'prompt_secret', prompt)
    configured = onboarding.run_onboarding(Console(file=StringIO()), directory=tmp_path/'cfg')
    assert len(prompts) == 1 and prompts[0]['required'] is True
    assert load_settings(tmp_path/'cfg').api_key == 'entered-key'
    assert onboarding.effective_key(configured) == 'entered-key'


@pytest.mark.parametrize('saved_config', [False, True])
def test_interactive_start_does_not_prompt_when_env_exists(tmp_path, monkeypatch, saved_config):
    work = tmp_path/'work'
    work.mkdir()
    (work/'.env').write_text('DEEPSEEK_API_KEY=workspace-key\n', encoding='utf-8')
    config = tmp_path/'model.yaml'
    config.write_text('provider: deepseek\nmodel: fixture\n', encoding='utf-8')
    directory = tmp_path/'cfg'
    if saved_config:
        save_settings(ProviderSettings(provider='deepseek', model='fixture', api_key='terminal-key'), directory)
    monkeypatch.setattr(cli.sys, 'stdin', type('TTY', (), {'isatty':lambda self: True})())
    monkeypatch.setattr(cli, 'Console', lambda **kw: Console(file=StringIO(), force_terminal=False))
    inputs = iter(['hello', '/exit'])
    monkeypatch.setattr('rich.console.Console.input', lambda *a, **kw: next(inputs))
    def unexpected(*args, **kwargs):
        raise AssertionError('unexpected onboarding')
    monkeypatch.setattr(onboarding, 'run_onboarding', unexpected)
    real_build = cli.build_model
    selected = []
    def capture(*args, **kwargs):
        model, _ = real_build(*args, **kwargs)
        selected.append(model.client.api_key)
        model.client.close()
        return FakeModel(), False
    monkeypatch.setattr(cli, 'build_model', capture)
    assert cli.main(['--workspace',str(work),'--config',str(config),'--config-dir',str(directory),'--no-agent-docs']) == 0
    assert selected == ['workspace-key']
    if not saved_config:
        assert not (directory/'config.json').exists()


def test_model_settings_shows_effective_source_not_saved_fallback(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'env-123456789-key')
    output = StringIO()
    CliView(Console(file=output, width=120, force_terminal=False)).render_model_settings(
        ProviderSettings(provider='deepseek', model='fixture', api_key='saved-123456-key'))
    text = output.getvalue()
    assert '环境变量 / .env' in text
    assert 'env-' in text and 'saved-' not in text
    assert 'env-123456789-key' not in text
