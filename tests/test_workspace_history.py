"""默认工作区、集中历史与跨目录恢复的 CLI 验收。"""
import json
from pathlib import Path

import pytest

from genesisai.app import cli
from genesisai.core.state.store import Store, application_state_root, default_workspace
from genesisai.shared.messages import Response
from genesisai.shared.security import Access, ToolError
from test_acceptance import FakeModel, call, response


def sessions():
    return [json.loads(p.read_text(encoding='utf-8'))
            for p in (application_state_root() / 'sessions').glob('*.json')]


def test_default_cli_creates_files_and_full_history_in_default_workspace(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    model = FakeModel(response(call('editor', {
        'operation': 'create', 'path': 'result.txt', 'content': 'hello', 'source_refs': [],
    })), Response(content='done'))
    monkeypatch.setattr(cli, 'build_model', lambda _: (model, False))
    assert cli.main(['--no-agent-docs', '--permissions', 'auto', '--prompt', 'create file']) == 0
    assert (default_workspace() / 'result.txt').read_text() == 'hello'
    saved, = sessions()
    assert Path(saved['workspace']) == default_workspace()
    events = [json.loads(line) for line in (
        application_state_root() / 'logs' / f"{saved['id']}.jsonl"
    ).read_text(encoding='utf-8').splitlines()]
    assert {'model_input', 'model_output', 'tool_execution'} <= {e['event'] for e in events}
    assert {e['payload']['phase'] for e in events if e['event'] == 'tool_execution'} == {'start', 'end'}
    assert not (tmp_path / '.genesis').exists()


def test_different_projects_share_history_and_resume_original_workspace(monkeypatch, tmp_path):
    projects = [tmp_path / 'project a', tmp_path / 'project b']
    model = FakeModel(Response(content='answer a'), Response(content='answer b'), Response(content='resumed'))
    monkeypatch.setattr(cli, 'build_model', lambda _: (model, False))
    environments = []
    monkeypatch.setattr(cli, 'load_environment', lambda explicit, workspace: environments.append(workspace))
    for project in projects:
        assert cli.main(['--workspace', str(project), '--no-agent-docs', '--prompt', project.name]) == 0
        assert not (project / '.genesis').exists()
    saved = sessions()
    assert len(saved) == 2
    first = next(s for s in saved if Path(s['workspace']) == projects[0])
    monkeypatch.chdir(projects[1])
    assert cli.main(['--session', first['id'], '--no-agent-docs', '--prompt', 'continue']) == 0
    assert environments == [projects[0], projects[1], projects[0]]
    assert len(sessions()) == 2
    assert str(projects[0]) in model.contexts[-1][0].content
    assert any(m.content == 'answer a' for m in model.contexts[-1])
    assert not any(m.content == 'answer b' for m in model.contexts[-1])


def test_resume_rejects_workspace_change_without_overwriting_history(monkeypatch, tmp_path):
    project = cli.select_workspace(tmp_path / 'original')
    store = Store(application_state_root(), workspace=project)
    before = store.path.read_bytes()
    monkeypatch.setattr(cli, 'build_model', lambda _: pytest.fail('model must not run'))
    assert cli.main(['--session', store.id, '--workspace', str(tmp_path / 'other'), '--prompt', 'continue']) == 2
    assert store.path.read_bytes() == before


def test_resume_missing_project_does_not_recreate_it(monkeypatch, tmp_path):
    project = cli.select_workspace(tmp_path / 'original')
    store = Store(application_state_root(), workspace=project)
    project.rmdir()
    monkeypatch.setattr(cli, 'build_model', lambda _: pytest.fail('model must not run'))
    assert cli.main(['--session', store.id, '--prompt', 'continue']) == 2
    assert not project.exists()
    assert cli.main(['--session', store.id, '--workspace', str(project), '--prompt', 'continue']) == 2
    assert not project.exists()


def test_history_is_not_accessible_as_default_workspace_task_files():
    workspace = cli.select_workspace(None)
    store = Store(application_state_root(), workspace=workspace)
    access = Access([workspace], workspace, store.root, workspace_root=workspace)
    with pytest.raises(ToolError, match='状态文件'):
        access.read(store.path)
    with pytest.raises(ToolError, match='状态文件'):
        access.write('.genesis/overwrite.txt')


def test_default_workspace_file_conflict_is_reported(tmp_path, capsys):
    default_workspace().parent.mkdir(parents=True)
    default_workspace().write_text('occupied')
    assert cli.main(['--prompt', 'hello']) == 2
    assert '工作区必须是目录' in capsys.readouterr().out
