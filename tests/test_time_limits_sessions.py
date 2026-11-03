import json
import time
import os
from io import StringIO

from rich.console import Console

from genesisai.agent.loop import Runner
from genesisai.app import cli, session_picker
from genesisai.core.state.store import Store, application_state_root
from genesisai.shared.messages import Response
from test_acceptance import FakeModel
from test_development_continuation import development_env


def test_profile_changes_do_not_override_explicit_time_limit(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    runner = Runner(FakeModel(Response(content='answer')), runtime, seconds=600, request_timeout=120)
    assert runner.start('hello')['status'] == 'completed'
    assert 599 <= store.data['deadline'] - store.data['run_runtime']['started_at'] <= 601
    deadline = store.data['deadline']
    runtime.research.switch_profile('web_quick')
    assert store.data['deadline'] == deadline
    assert store.data['run_runtime']['request_timeout_seconds'] == 120


def test_retry_uses_remaining_task_time(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    seen = []
    class InternalServerError(Exception):
        pass
    class Model:
        def chat(self, messages, tools=None, **options):
            seen.append(options['timeout'])
            if len(seen) == 1:
                store.data['deadline'] = time.time() + 5
                raise InternalServerError('busy')
            return Response(content='answer')
    result = Runner(Model(), runtime, seconds=300, request_timeout=60).start('hello')
    assert result['status'] == 'completed'
    assert seen[0] == 60 and 0 < seen[1] <= 5


def test_expired_task_does_not_retry_or_execute_returned_tools(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    class APITimeoutError(Exception):
        pass
    class Model:
        calls = 0
        def chat(self, *args, **kwargs):
            self.calls += 1
            store.data['deadline'] = time.time() - 1
            raise APITimeoutError('timeout')
    model = Model()
    result = Runner(model, runtime, seconds=300).start('hello')
    assert result['status'] == 'limit_reached'
    assert '300' in result['answer'] and model.calls == 1


def test_verified_work_gets_recorded_summary_on_request_timeout(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    runner = Runner(FakeModel(), runtime)
    store.data['run_runtime'].update(project_changed=True, verification_succeeded=True, verification_required=False)
    result = runner.finish_timeout(task_expired=False)
    assert result['status'] == 'completed'
    assert '最终说明请求超时' in result['answer']
    assert store.data['messages'][-1]['content'] == result['answer']
    store.data['run_runtime']['verification_required'] = True
    assert runner.finish_timeout(task_expired=False)['status'] == 'failed'


def test_menu_restores_other_workspace_history_and_logs(tmp_path, monkeypatch):
    old_workspace = tmp_path / 'old'
    old_workspace.mkdir()
    restored = Store(application_state_root(), workspace=old_workspace)
    restored.data.update(grants=[str(old_workspace)], output=str(old_workspace), status='completed')
    restored.data['messages'] = [{'role': 'user', 'content': 'previous question'},
                                 {'role': 'assistant', 'content': 'previous answer'}]
    restored.save()
    model = FakeModel(Response(content='continued answer'))
    monkeypatch.setattr(cli, 'build_model', lambda _: (model, False))
    monkeypatch.setattr(cli, 'select', lambda *a, **k: 0)
    monkeypatch.setattr(session_picker, 'select', lambda *a, **k: 0)
    prompts = iter(['/', 'continue please', '/exit'])
    monkeypatch.setattr(Console, 'input', lambda *a, **k: next(prompts))
    assert cli.main(['--workspace', str(tmp_path / 'new'), '--seconds', '600', '--request-timeout', '120']) == 0
    assert any(m.content == 'previous answer' for m in model.contexts[0])
    data = json.loads(restored.path.read_text(encoding='utf-8'))
    assert data['workspace'] == str(old_workspace)
    assert data['answer'] == 'continued answer'
    assert data['run_runtime']['execution_seconds_limit'] == 600
    events = [json.loads(line) for line in (restored.root / 'logs' / f'{restored.id}.jsonl').read_text(encoding='utf-8').splitlines()]
    assert any(e['event'] == 'session_open' and e['payload']['resumed'] for e in events)


def test_cancel_session_picker_keeps_active_session(tmp_path, monkeypatch):
    model = FakeModel(Response(content='answer'))
    monkeypatch.setattr(cli, 'build_model', lambda _: (model, False))
    monkeypatch.setattr(cli, 'choose_session', lambda *a: None)
    prompts = iter(['/sessions', 'hello', '/exit'])
    monkeypatch.setattr(Console, 'input', lambda *a, **k: next(prompts))
    assert cli.main(['--workspace', str(tmp_path / 'work')]) == 0
    assert len(list((application_state_root() / 'sessions').glob('*.json'))) == 1


def test_picker_skips_corrupt_file_without_rewriting(tmp_path):
    root = tmp_path / 'state'
    (root / 'sessions').mkdir(parents=True)
    path = root / 'sessions' / 'session_broken.json'
    path.write_text('{broken', encoding='utf-8')
    console = Console(file=StringIO())
    assert session_picker.choose_session(console, root, 'session_current') is None
    assert path.read_text() == '{broken'


def test_session_switch_loads_selected_workspace_environment(tmp_path, monkeypatch):
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    name = 'GENESIS_TEST_ENV_SOURCE'
    monkeypatch.delenv(name, raising=False)
    (first / '.env').write_text(f'{name}=first\n', encoding='utf-8')
    (second / '.env').write_text(f'{name}=second\n', encoding='utf-8')
    previous = Store(application_state_root(), workspace=second)
    previous.data.update(grants=[str(second)], output=str(second))
    previous.save()
    seen = []
    def build(_):
        seen.append(os.environ.get(name))
        return FakeModel(), False
    monkeypatch.setattr(cli, 'build_model', build)
    monkeypatch.setattr(cli, 'choose_session', lambda *a: previous.id)
    prompts = iter(['/sessions', '/exit'])
    monkeypatch.setattr(Console, 'input', lambda *a, **k: next(prompts))
    try:
        assert cli.main(['--workspace', str(first)]) == 0
        assert seen == ['first', 'second']
    finally:
        os.environ.pop(name, None)
