import json
from types import SimpleNamespace

import pytest

from genesisai.app.command_input import CommandHistory
from genesisai.app.job_input_commands import cli_input_text, execute_job_input
from test_file_tool_jobs import runtime  # noqa: F401 - shared fixture
from test_interactive_job_input import start, until


def test_job_input_body_is_not_retained_in_log_text_or_command_history():
    raw = '/job-input ' + json.dumps({'job_id': 'job_fixture', 'action': 'write', 'text': 'synthetic-private-value'})
    assert 'synthetic-private-value' not in cli_input_text(raw)
    assert cli_input_text('/status') == '/status'
    history = CommandHistory([raw, '/status'])
    history.append_string(raw)
    history.append_string('/help')
    assert list(history.load_history_strings()) == ['/help', '/status']
    assert all('synthetic-private-value' not in value for value in history.get_strings())


def test_explicit_cli_input_does_not_commit_or_overwrite_runner_state(runtime, monkeypatch):
    job_id = start(runtime, "import sys\nprint('READY>',end='',flush=True)\nprint('GOT:'+sys.stdin.readline(),flush=True)\n", 'pipe')
    until(lambda: 'READY>' in runtime.jobs.output(job_id)['text'])
    def forbidden():
        raise AssertionError('CLI input must not commit the active runner Session')
    monkeypatch.setattr(runtime.store, 'commit', forbidden)
    result = execute_job_input(runtime.store, runtime.logger,
        '/job-input ' + json.dumps({'job_id': job_id, 'action': 'write', 'text': 'CLI_OK', 'append_newline': True}))
    assert result['status'] in {'accepted', 'sent'}
    assert runtime.jobs.wait(job_id, 10)['status'] == 'succeeded'
    assert 'GOT:CLI_OK' in runtime.jobs.output(job_id)['text']


@pytest.mark.parametrize('command', ['/job-input', '/job-input []', '/job-input {broken'])
def test_invalid_control_input_is_rejected_without_echoing_body(command):
    with pytest.raises(ValueError):
        execute_job_input(SimpleNamespace(id='session', root='unused'), None, command)
