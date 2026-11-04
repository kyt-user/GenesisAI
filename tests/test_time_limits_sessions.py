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
        def stream_chat(self, messages, tools=None, **options):
            seen.append(options['timeout'])
            if len(seen) == 1:
                store.data['deadline'] = time.time() + 5
                raise InternalServerError('busy')
            yield Response(content='answer')
    result = Runner(Model(), runtime, seconds=300, request_timeout=60).start('hello')
    assert result['status'] == 'completed'
    assert seen[0] == 60 and 0 < seen[1] <= 5


def test_expired_task_does_not_retry_or_execute_returned_tools(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    class APITimeoutError(Exception):
        pass
    class Model:
        calls = 0
        def stream_chat(self, *args, **kwargs):
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
        return FakeModel(Response(content='done')), False
    monkeypatch.setattr(cli, 'build_model', build)
    monkeypatch.setattr(cli, 'choose_session', lambda *a: previous.id)
    prompts = iter(['first task', '/sessions', 'second task', '/exit'])
    monkeypatch.setattr(Console, 'input', lambda *a, **k: next(prompts))
    try:
        assert cli.main(['--workspace', str(first)]) == 0
        assert seen == ['first', 'second']
    finally:
        os.environ.pop(name, None)


def test_home_and_history_are_local_and_do_not_create_sessions(tmp_path, monkeypatch):
    work = tmp_path / 'poems'
    work.mkdir()
    root = application_state_root()
    saved = Store(root, workspace=work)
    question = '给我推荐一首古诗，并说明原因'
    answer = '推荐王维的《鹿柴》。'
    saved.data['messages'] = [{'role': 'user', 'content': question},
                              {'role': 'assistant', 'content': answer}]
    saved.save()
    empty = Store(root, workspace=work)
    before = {s.id: (s.path.read_bytes(), s.path.stat().st_mtime_ns) for s in (empty, saved)}
    output = StringIO()
    monkeypatch.setattr(cli, 'Console', lambda **kw: Console(file=output, width=120))
    def unexpected(*args, **kwargs):
        raise AssertionError('Browsing must be local')
    monkeypatch.setattr(cli, 'build_model', unexpected)
    monkeypatch.setattr(cli, 'load_settings', unexpected)
    monkeypatch.setattr(cli, 'choose_session', lambda *args: saved.id)
    inputs = iter(['/sessions', '/history', '/new', '/exit'])
    monkeypatch.setattr(Console, 'input', lambda *args, **kw: next(inputs))
    assert cli.main(['--workspace', str(work)]) == 0
    assert answer in output.getvalue() and question in output.getvalue()
    assert empty.id not in output.getvalue()
    inputs = iter(['/exit'])
    assert cli.main(['--session', saved.id]) == 0
    for path in (root / 'sessions').glob('*.json'):
        assert before[path.stem] == (path.read_bytes(), path.stat().st_mtime_ns)


def test_remote_model_answers_first_message_directly(tmp_path, monkeypatch):
    model = FakeModel(Response(content='王维的《鹿柴》。'))
    monkeypatch.setattr(cli, 'build_model', lambda _: (model, True))
    inputs = iter(['推荐一首古诗', '/exit'])
    monkeypatch.setattr(Console, 'input', lambda *args, **kw: next(inputs))
    assert cli.main(['--workspace', str(tmp_path / 'poems')]) == 0
    saved, = (application_state_root() / 'sessions').glob('*.json')
    messages = json.loads(saved.read_text(encoding='utf-8'))['messages']
    assert [m['content'] for m in messages] == ['推荐一首古诗', '王维的《鹿柴》。']
    assert len(model.contexts) == 1


def test_recent_titles_and_workspace_filter(tmp_path):
    work = tmp_path / 'work'
    root = application_state_root()
    for folder in (work, tmp_path / 'other'):
        item = Store(root, workspace=folder)
        item.data['messages'] = [{'role': 'user', 'content': '最初的问题'}, {'role': 'user', 'content': '继续'}]
        item.save()
    Store(root, workspace=work)
    console = Console(file=StringIO())
    entries = session_picker.session_entries(console, root, workspace=work)
    assert len(entries) == 1 and entries[0][1] == '最初的问题'
    assert len(session_picker.session_entries(console, root)) == 2


def test_reopened_poem_followup_belongs_to_original_session(tmp_path, monkeypatch):
    work = tmp_path / 'poems'
    work.mkdir()
    store = Store(application_state_root(), workspace=work)
    answer = '我推荐王维的《鹿柴》。'
    store.data['messages'] = [{'role': 'user', 'content': '推荐一首古诗'}, {'role': 'assistant', 'content': answer}]
    store.save()
    model = FakeModel(Response(content='王维的《鹿柴》。'))
    monkeypatch.setattr(cli, 'build_model', lambda _: (model, False))
    monkeypatch.setattr(cli, 'choose_session', lambda *args: store.id)
    stream = StringIO()
    monkeypatch.setattr(cli, 'Console', lambda **kw: Console(file=stream, width=100))
    prompts = iter(['/sessions', '你前面推荐了哪首诗？', '/exit'])
    def read(*args, **kwargs):
        value = next(prompts)
        if value.startswith('你前面'):
            assert answer in stream.getvalue() and not model.contexts
        return value
    monkeypatch.setattr(Console, 'input', read)
    assert cli.main(['--workspace', str(work)]) == 0
    saved, = (store.root / 'sessions').glob('*.json')
    assert saved == store.path
    data = json.loads(saved.read_text(encoding='utf-8'))
    assert len(data['messages']) == 4 and data['status'] == 'completed'
    assert any(m.content == answer for m in model.contexts[0])


def test_project_memory_prompt_waits_for_development_request(tmp_path, monkeypatch):
    work = tmp_path / 'work'
    model = FakeModel(Response(content='王维'), Response(content='完成'))
    monkeypatch.setattr(cli.sys, 'stdin', type('TTY', (), {'isatty': lambda self: True})())
    monkeypatch.setattr(cli, 'Console', lambda **kw: Console(file=StringIO(), force_terminal=False))
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-key')
    monkeypatch.setattr(cli, 'build_model', lambda _: (model, False))
    prompts = iter(['推荐一首古诗', '实现一个 Python 函数', '/exit'])
    monkeypatch.setattr(Console, 'input', lambda *args, **kw: next(prompts))
    offered = []
    def consent(self, purpose, title):
        offered.append((purpose, len(model.contexts)))
        return 'allow'
    monkeypatch.setattr(cli.CliView, 'request_consent', consent)
    assert cli.main(['--workspace', str(work)]) == 0
    assert offered == [('创建项目任务记录', 1)]
    assert (work / 'agent_docs').is_dir()
