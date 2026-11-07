"""Full project reachability, declared entrypoint priority and package paging."""
import json
from pathlib import Path

from genesisai.core.extensions.tools.shell.project_detection import automatic_project_command, inspect_project
from genesisai.core.extensions.tools.shell.python_support import inspect_python_project, automatic_frontend_command
from genesisai.core.tools.base import SchemaValidationError


def test_counts_and_dependencies_reach_beyond_old_caps(tmp_path):
    for index in range(5010):
        (tmp_path / f'source_{index:04d}.py').write_text('value = 1\n', encoding='utf-8')
    tests = tmp_path / 'tests'; tests.mkdir()
    for index in range(51):
        (tests / f'test_{index:03d}.py').write_text('# fixture\n', encoding='utf-8')
    (tests / 'test_999.py').write_text('# prefix\n' * 1200 + 'import unittest\n', encoding='utf-8')
    dependencies = [f'dependency_{index}' for index in range(131)]
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="large"\ndependencies=' + json.dumps(dependencies), encoding='utf-8')
    details = inspect_python_project(tmp_path)
    assert details['python_file_count'] == 5062 and details['test_file_count'] == 52
    assert details['test_framework'] == 'unittest'
    assert details['dependencies'] == dependencies
    assert details['coverage']['complete']
    page = inspect_project(tmp_path, section='dependencies', offset=100, limit=31)
    assert page['items'][-1] == 'dependency_130' and page['next_offset'] is None


def test_declared_node_test_precedes_incidental_python_and_static_html(tmp_path):
    (tmp_path / 'helper.py').write_text('print("helper")', encoding='utf-8')
    (tmp_path / 'index.html').write_text('<!doctype html><title>demo</title>', encoding='utf-8')
    (tmp_path / 'package.json').write_text(json.dumps({'scripts': {'test': 'node --test'}, 'packageManager': 'pnpm@10'}), encoding='utf-8')
    command, details = automatic_project_command(tmp_path)
    assert command == ['pnpm', 'test']
    assert details['test_detection_reason'] == 'package.json:scripts.test'


def test_nested_packages_are_all_reachable_without_picking_one_silently(tmp_path):
    for index in range(127):
        package = tmp_path / f'package_{index:03d}'; package.mkdir()
        (package / 'package.json').write_text('{"scripts":{"test":"node --test"}}', encoding='utf-8')
    page = inspect_project(tmp_path, section='packages', offset=100, limit=30)
    assert page['total'] == 127 and len(page['items']) == 27 and page['next_offset'] is None
    assert page['items'][-1]['path'] == 'package_126'
    command, details = automatic_project_command(tmp_path)
    assert command == [] and details['package_count'] == 127


def test_multiple_declared_root_entries_require_explicit_choice(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[tool.pytest.ini_options]\n', encoding='utf-8')
    (tmp_path / 'package.json').write_text('{"scripts":{"test":"node --test"}}', encoding='utf-8')
    command, details = automatic_project_command(tmp_path)
    assert command == [] and len(details['test_commands']) == 2


def test_frontend_inventory_and_scanner_exclusions_are_complete(tmp_path):
    for index in range(121):
        (tmp_path / f'page_{index:03d}.html').write_text('<!doctype html><title>x</title>', encoding='utf-8')
    ignored = tmp_path / 'node_modules'; ignored.mkdir()
    (ignored / 'hidden.py').write_text('import pytest', encoding='utf-8')
    command, details = automatic_frontend_command(tmp_path)
    assert command and len(details['html_files']) == 121
    summary = inspect_project(tmp_path)
    assert summary['python_file_count'] == 0
    assert summary['sections']['html_files'] == 121
    assert 'node_modules' in summary['coverage']['excluded_directories']


def test_inspection_uses_independent_job_and_old_dispatch_is_removed(tmp_path):
    import pytest
    from genesisai.core.persistence.session import Session
    from genesisai.core.tools.tool_runtime import ToolRuntime
    from genesisai.shared.security import Access
    root = tmp_path / 'workspace'; root.mkdir()
    (root / 'source.py').write_text('x = 1', encoding='utf-8')
    store = Session(tmp_path / 'state', workspace=root)
    store.record.apply(grants=[str(root)], output=str(root)); store.commit()
    runtime = ToolRuntime(store, Access([root], root, store.root, workspace_root=root), providers=[])
    try:
        result = runtime.execute({'id': 'inspect', 'name': 'inspect_project', 'arguments': '{}'})
        assert result['ok'], result
        if result['data'].get('job_id'):
            result = runtime.execute({'id': 'collect', 'name': 'read_job', 'arguments': json.dumps({'job_id': result['data']['job_id'], 'wait_seconds': 10})})
            assert result['ok'], result
            details = result['data']['result']
        else:
            details = result['data']
        assert details['python_file_count'] == 1
        assert runtime.jobs.status()['jobs'][0]['kind'] == 'builtin_tool'
        with pytest.raises(SchemaValidationError):
            runtime.registry.get('run_commands').validate_arguments({'operation': 'inspect'})
    finally:
        runtime.jobs.stop_all()
        runtime.close()
