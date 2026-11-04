"""真实修改、批准、测试、继续对话及终端流式显示的回归。"""
import sys
from io import StringIO

import pytest
from rich.console import Console

from genesisai.agent.loop import Runner
from genesisai.app.terminal_view import CliView
from genesisai.core.state.store import Store, sha
from genesisai.shared.messages import Response
from test_acceptance import FakeModel, call
from test_development_completion_gate import tool_response
from test_development_continuation import development_env


def test_approved_code_change_and_tests_finish_then_allow_followup(tmp_path):
    workspace, _, _, store, runtime = development_env(tmp_path)
    source = workspace / 'calc.py'
    source.write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
    runtime.set_permission("user")
    model = FakeModel(
        tool_response('read_files', {'paths': ['calc.py', 'agent_docs/index.json']}, 'read'),
        tool_response('editor', {'operation': 'patch', 'path': 'calc.py',
            'expected_sha256': sha(source),
            'patch': '--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n'}, 'patch'),
        tool_response('editor', {'operation': 'create', 'path': 'tests/test_calc.py',
            'content': 'import unittest\nfrom calc import add\nclass TestAdd(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n'}, 'test_file'),
        tool_response('run_commands', {'command': [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                                      'cwd': str(workspace)}, 'test_run'),
        Response(content='已修复并通过测试。'),
        Response(content='上一轮修改和测试均成功。'),
    )
    runner = Runner(model, runtime)
    result = runner.start('开始')
    assert result['status'] == 'awaiting_confirmation'
    result = runner.confirm(True)
    assert store.data['run_runtime']['project_changed'] is True
    while result['status'] == 'awaiting_confirmation':
        result = runner.confirm(True)
    assert result['status'] == 'completed'
    assert store.data['run_runtime']['verification_succeeded'] is True
    assert store.data['run_runtime']['recovery_counts'].get('tool_required', 0) == 0
    assert store.data['calls']['test_run']['result']['data']['exit_code'] == 0
    assert 'return a + b' in source.read_text()
    old_ref = next(ref for ref, data in store.data['sources'].items() if data['path'] == str(source))
    assert store.data['sources'][old_ref]['stale'] is True
    assert runner.start('请总结上一轮操作')['status'] == 'completed'
    assert '历史文件快照已失效' in model.contexts[-1][0].content


@pytest.mark.parametrize('deleted', [False, True])
def test_changed_files_keep_history_and_require_fresh_evidence(tmp_path, deleted):
    workspace, _, _, store, runtime = development_env(tmp_path)
    path = workspace / 'note.txt'
    path.write_text('old', encoding='utf-8')
    original_hash = sha(path)
    ref = store.source(kind='file', path=str(path), sha256=original_hash, title=path.name)
    artifact = store.artifact(path)
    if deleted:
        path.unlink()
    else:
        path.write_text('new', encoding='utf-8')
    restored = Store(store.root, store.id, workspace=workspace)
    restored.verify_sources()
    assert restored.data['sources'][ref]['sha256'] == original_hash
    assert restored.data['sources'][ref]['stale'] is True
    assert restored.data['artifacts'][artifact]['stale'] is True
    runtime.store = restored
    runner = Runner(FakeModel(), runtime)
    assert not runner._answer_refs_valid(f'[{ref}]')
    if not deleted:
        new_ref = restored.source(kind='file', path=str(path), sha256=sha(path), title=path.name)
        assert new_ref != ref
        assert runner._answer_refs_valid(f'[{new_ref}]')


def test_terminal_stream_is_not_erased_by_live_spinner():
    sink = StringIO()
    view = CliView(Console(file=sink, width=100, force_terminal=True,
                          legacy_windows=False, _environ={'TERM': 'xterm-256color'}))
    with view.working('BUSY'):
        for chunk in ['FIRST', 'SECOND', 'THIRD']:
            view.render_progress('stream_token', {'text': chunk})
    body = sink.getvalue().split('FIRST', 1)[1]
    assert body.startswith('SECONDTHIRD')
    assert '\x1b[2K' not in body


def test_created_then_edited_artifact_uses_recorded_change_but_detects_external_tampering(tmp_path):
    workspace, _, _, store, runtime = development_env(tmp_path)
    assert runtime.execute(call('editor', {'operation': 'create', 'path': 'note.txt', 'content': 'old\n'}, 'create'))['ok']
    path = workspace / 'note.txt'
    assert runtime.execute(call('editor', {'operation': 'patch', 'path': 'note.txt', 'expected_sha256': sha(path),
        'patch': '--- a/note.txt\n+++ b/note.txt\n@@ -1 +1 @@\n-old\n+new\n'}, 'patch'))['ok']
    store.verify_artifacts()
    path.write_text('external change', encoding='utf-8')
    with pytest.raises(ValueError, match='产物'):
        store.verify_artifacts()


def test_runtime_rejection_is_visible_after_streamed_success():
    sink = StringIO()
    view = CliView(Console(file=sink, width=120, force_terminal=False))
    view.render_progress('stream_token', {'text': '模型声称已经完成'})
    view.render_result({'status': 'partial', 'answer': '测试失败，需要继续修复'}, {'rounds': 2})
    assert '测试失败，需要继续修复' in sink.getvalue()
    assert '部分完成' in sink.getvalue()
