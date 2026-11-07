"""Verification selection follows risk and actual command outcomes."""
import sys
import pytest

from genesisai.core.development.policy import DevelopmentPolicy
from test_file_tool_job_audit import runtime, execute


def test_document_changes_do_not_require_project_tests():
    policy = DevelopmentPolicy()
    for path in ('README.md', 'guide.rst', 'report.docx'):
        result = {'data': {}, 'observations': [{'kind': 'resource_changed', 'path': path}]}
        assert not policy.requires_change_verification(result, run_state={'protocols': ['coding']})
    result['observations'][0]['path'] = 'service.py'
    assert policy.requires_change_verification(result, run_state={'protocols': ['coding']})


def test_static_check_command_produces_actual_verification_and_failure(runtime):
    source = runtime.store.workspace / 'module.py'
    source.write_text('x = 2\n')
    for text, expected in [('x = 2\n', True), ('x = (\n', False)]:
        source.write_text(text)
        result = execute(runtime, 'run_commands', {'command': [sys.executable, '-m', 'py_compile', str(source)], 'verification': 'static'}, 'check_' + str(expected))
        assert result['ok'], result
        assert result['data']['passed'] is expected
        assert any(item['kind'] == 'verification' and item['passed'] is expected for item in result['observations'])


def test_background_build_verification_is_collected_from_same_job(runtime):
    result = execute(runtime, 'run_commands', {'command': [sys.executable, '-c', 'import time; time.sleep(.5); print("build fixture")'], 'verification': 'build', 'background': True}, 'build')
    assert result['ok'], result
    result = execute(runtime, 'read_job', {'job_id': result['data']['job_id'], 'wait_seconds': 10}, 'build_result')
    assert result['ok'] and result['data']['passed'], result
    assert any(item['kind'] == 'verification' and item['passed'] for item in result['observations'])


@pytest.mark.parametrize('tool,args', [('inspect_capabilities', {'protocol': True}), ('discover_tools', {'query': 'editor'})])
def test_discovery_is_valid_preparation_evidence_but_not_verification(runtime, tool, args):
    import pytest
    from genesisai.shared.security import ToolError
    runtime.store.record.run_id = 'run_preparation'
    runtime.store.commit()
    runtime.tasks.begin_task('构建并验证工具', run_id='run_preparation')
    runtime.tasks.update_plan(expected_revision=0, upsert=[
        {'id': 'read', 'kind': 'step', 'text': '读取能力协议或发现工具'},
        {'id': 'test', 'kind': 'acceptance', 'text': '实际测试通过', 'requires_verification': True}])
    result = execute(runtime, tool, args, 'preparation')
    assert result['ok'], result
    runtime.tasks.observe([], call_id='preparation')
    assert not runtime.tasks.has_execution_evidence()
    runtime.tasks.update_plan(expected_revision=1, upsert=[{'id': 'read', 'status': 'completed', 'evidence_calls': ['preparation']}])
    with pytest.raises(ToolError, match='实际验证'):
        runtime.tasks.update_plan(expected_revision=2, upsert=[{'id': 'test', 'status': 'completed', 'evidence_calls': ['preparation']}])
    assert runtime.tasks.completion_issue()


@pytest.mark.parametrize('name', ['guide.rst', 'guide.adoc'])
def test_document_create_does_not_mislabel_it_as_source_verification(runtime, name):
    result = execute(runtime, 'editor', {'operation': 'create', 'path': name, 'content': 'Document body'}, 'doc')
    assert result['ok'], result
    assert result['data']['requires_verification'] is False
    assert not DevelopmentPolicy().requires_change_verification(result, run_state={'protocols': ['coding']})
