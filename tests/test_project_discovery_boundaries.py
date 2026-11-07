"""Boundary and completeness regressions for full project discovery."""
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from genesisai.core.extensions.tools.shell import project_detection as detection
from genesisai.core.extensions.tools.shell import python_support as python
from genesisai.shared.security import ToolError


@pytest.mark.parametrize(('manifest', 'content'), [('setup.cfg', '[tool:pytest]\naddopts=-q\n'), ('tox.ini', '[pytest]\naddopts=-q\n')])
def test_declared_ini_pytest_precedes_unittest_heuristic(tmp_path, manifest, content):
    (tmp_path / manifest).write_text(content, encoding='utf-8')
    (tmp_path / 'test_one.py').write_text('import unittest\n', encoding='utf-8')
    command, details = detection.automatic_project_command(tmp_path)
    assert command[-3:] == ['-m', 'pytest', '-q']
    assert details['test_detection_reason'].startswith(manifest + ':')


@pytest.mark.parametrize(('filename', 'content'), [('helper.py', 'x=1'), ('index.html', '<html></html>')])
def test_selection_reuses_one_inventory(tmp_path, monkeypatch, filename, content):
    (tmp_path / filename).write_text(content, encoding='utf-8')
    original = detection.project_files
    calls = []
    def scan(root):
        calls.append(root)
        return original(root)
    monkeypatch.setattr(detection, 'project_files', scan)
    monkeypatch.setattr(python, 'project_files', lambda root: pytest.fail('duplicate inventory scan'))
    command, _ = detection.automatic_project_command(tmp_path)
    assert command and len(calls) == 1


def test_root_declaration_and_nested_packages_remain_distinct(tmp_path):
    (tmp_path / 'package.json').write_text('{"scripts":{"test":"node --test"}}', encoding='utf-8')
    nested = tmp_path / 'python-package'; nested.mkdir()
    (nested / 'tox.ini').write_text('[pytest]\n', encoding='utf-8')
    command, details = detection.automatic_project_command(tmp_path)
    assert command == ['npm', 'test'] and details['nested_packages'] == 1
    packages = detection.inspect_project(tmp_path, section='packages')['items']
    assert [(item['path'], item['test_commands'][0]['framework']) for item in packages] == [('.', 'npm'), ('python-package', 'pytest')]


@pytest.mark.parametrize('file_link', [False, True])
def test_compileall_cannot_reenter_skipped_links(tmp_path, file_link):
    root = tmp_path / 'workspace'; root.mkdir()
    external = tmp_path / 'external'; external.mkdir()
    (external / 'secret.py').write_text('raise RuntimeError("outside")', encoding='utf-8')
    (root / 'helper.py').write_text('x=1', encoding='utf-8')
    link = root / 'src'
    if file_link:
        link.mkdir()
        (link / 'safe.py').write_text('x=1', encoding='utf-8')
        link = link / 'linked.py'
    try:
        link.symlink_to(external / 'secret.py' if file_link else external, target_is_directory=not file_link)
    except OSError as exc:
        pytest.skip('OS does not permit this symlink fixture: ' + type(exc).__name__)
    command, details = detection.automatic_project_command(root)
    assert command == [] and 'compileall' in details['selection_issue']
    links = detection.inspect_project(root, section='skipped_links')
    assert links['items'] == [link.relative_to(root).as_posix()]
    if not file_link:
        assert 'src' not in details['source_roots']


def test_reparse_directory_is_not_reintroduced_as_source_root(tmp_path, monkeypatch):
    (tmp_path / 'helper.py').write_text('x=1', encoding='utf-8')
    linked = tmp_path / 'src'; linked.mkdir()
    (linked / 'outside.py').write_text('x=1', encoding='utf-8')
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == linked:
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value
    monkeypatch.setattr(Path, 'lstat', lstat)
    command, details = detection.automatic_project_command(tmp_path)
    assert command == [] and details['coverage']['skipped_links'] == ['src']
    assert 'src' not in details['source_roots']


def test_read_permission_failure_is_paged_and_blocks_heuristics(tmp_path, monkeypatch):
    denied = tmp_path / 'test_denied.py'; denied.write_text('import unittest', encoding='utf-8')
    original = Path.open
    def open_file(path, *args, **kwargs):
        if path == denied:
            raise PermissionError('fixture denied')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', open_file)
    command, details = detection.automatic_project_command(tmp_path)
    assert command == [] and not details['coverage']['complete']
    errors = detection.inspect_project(tmp_path, section='scan_errors')
    assert errors['items'] == [{'path': 'test_denied.py', 'error': 'PermissionError', 'operation': 'read'}]


def test_walk_permission_failure_has_explicit_coverage(tmp_path, monkeypatch):
    denied = tmp_path / 'restricted'; denied.mkdir()
    original = python.os.scandir
    def scandir(path):
        if Path(path) == denied:
            raise PermissionError(13, 'fixture denied', str(denied))
        return original(path)
    monkeypatch.setattr(python.os, 'scandir', scandir)
    summary = detection.inspect_project(tmp_path)
    errors = detection.inspect_project(tmp_path, section='scan_errors')['items']
    assert not summary['coverage']['complete'] and summary['coverage']['error_count'] == 1
    assert errors[0]['error'] == 'PermissionError' and errors[0]['operation'] == 'scan'


def test_all_links_errors_and_source_roots_are_pageable(tmp_path, monkeypatch):
    files = [f'package_{index:03d}/__init__.py' for index in range(151)]
    coverage = {'complete': False, 'excluded_directories': [], 'directories': ['.'] + [f'package_{index:03d}' for index in range(151)],
                'files_scanned': len(files), 'directories_scanned': 152,
                'skipped_links': [f'link_{index:03d}' for index in range(251)],
                'errors': [{'path': f'denied_{index:03d}', 'error': 'PermissionError', 'operation': 'scan'} for index in range(251)]}
    monkeypatch.setattr(detection, 'project_files', lambda root: copy.deepcopy((files, coverage)))
    summary = detection.inspect_project(tmp_path)
    assert not {'errors', 'skipped_links', 'directories'} & summary['coverage'].keys()
    for section, expected in [('skipped_links', coverage['skipped_links']), ('scan_errors', coverage['errors']), ('source_roots', coverage['directories'][1:])]:
        found, offset = [], 0
        while offset is not None:
            page = detection.inspect_project(tmp_path, section=section, offset=offset, limit=37)
            found.extend(page['items'])
            offset = page['next_offset']
        assert found == expected


def test_cancellation_between_read_blocks_is_not_swallowed(tmp_path, monkeypatch):
    target = tmp_path / 'test_large.py'; target.write_text('# text\n' * 40000, encoding='utf-8')
    original = Path.open
    reads, streams = [], []
    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, size):
            reads.append(size)
            return self.stream.read(size)
    def open_file(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        if path == target:
            streams.append(stream)
            return Reader(stream)
        return stream
    monkeypatch.setattr(Path, 'open', open_file)
    monkeypatch.setattr(python, 'cancellation_requested', lambda: len(reads) >= 2)
    coverage = {'errors': [], 'complete': True}
    with pytest.raises(ToolError) as error:
        list(python._text_chunks(tmp_path, target.name, coverage))
    assert error.value.error_type == 'user_cancelled'
    assert reads == [65536, 65536] and all(stream.closed for stream in streams)
    assert coverage['errors'] == []


@pytest.mark.parametrize('directory_link', [False, True])
def test_html_validator_uses_the_same_link_boundary(tmp_path, directory_link):
    from genesisai.core.extensions.tools.shell.static_validate import validate
    root = tmp_path / 'workspace'; root.mkdir()
    (root / 'safe.html').write_text('<!doctype html><html><body>safe</body></html>', encoding='utf-8')
    external = tmp_path / 'secret.html'; external.write_text('outside invalid markup', encoding='utf-8')
    try:
        (root / 'linked.html').symlink_to(external)
    except OSError as exc:
        pytest.skip('OS does not permit this symlink fixture: ' + type(exc).__name__)
    result = validate(root)
    assert result['passed'] and result['files'] == ['safe.html']
    assert result['coverage']['skipped_links'] == ['linked.html']


def test_html_validator_reports_every_unclosed_tag():
    from genesisai.core.extensions.tools.shell.static_validate import validate_html_text
    result = validate_html_text('<!doctype html><html><body><main><section><article><div><span>')
    assert not result['passed']
    assert any('html, body, main, section, article, div, span' in error for error in result['errors'])


def test_html_validator_read_errors_and_cancellation_are_not_success(tmp_path, monkeypatch):
    from genesisai.core.extensions.tools.shell import static_validate as validator
    target = tmp_path / 'index.html'; target.write_text('<!doctype html><html><body></body></html>', encoding='utf-8')
    original_open = Path.open
    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError('fixture denied')
        return original_open(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', denied)
        result = validator.validate(tmp_path)
        assert not result['passed'] and not result['coverage']['complete']
        assert any('PermissionError' in error for error in result['errors'])
    monkeypatch.setattr(python, 'cancellation_requested', lambda: True)
    with pytest.raises(ToolError) as error:
        validator.validate_html_text('<!doctype html><html><body></body></html>')
    assert error.value.error_type == 'user_cancelled'


@pytest.mark.parametrize(("linked_name", "declared"), [('pytest.ini', False), ('conftest.py', False), ('pytest.ini', True), ('conftest.py', True)])
def test_automatic_pytest_cannot_load_skipped_root_configuration(tmp_path, linked_name, declared):
    root = tmp_path / 'workspace'; root.mkdir()
    (root / 'test_one.py').write_text('import pytest\n', encoding='utf-8')
    external = tmp_path / linked_name
    external.write_text('[pytest]\n' if linked_name.endswith('.ini') else '# external conftest\n', encoding='utf-8')
    try:
        (root / linked_name).symlink_to(external)
    except OSError as exc:
        pytest.skip('OS does not permit this symlink fixture: ' + type(exc).__name__)
    if declared:
        (root / 'pyproject.toml').write_text('[tool.pytest.ini_options]\n', encoding='utf-8')
    command, details = detection.automatic_project_command(root)
    assert command == [] and linked_name in details['selection_issue']


def test_unrelated_declared_runner_is_not_blocked_by_pytest_config_link(tmp_path):
    root = tmp_path / 'workspace'; root.mkdir()
    (root / 'package.json').write_text('{"scripts":{"test":"node --test"}}', encoding='utf-8')
    external = tmp_path / 'pytest.ini'; external.write_text('[pytest]\n', encoding='utf-8')
    try:
        (root / 'pytest.ini').symlink_to(external)
    except OSError as exc:
        pytest.skip('OS does not permit this symlink fixture: ' + type(exc).__name__)
    command, _ = detection.automatic_project_command(root)
    assert command == ['npm', 'test']
