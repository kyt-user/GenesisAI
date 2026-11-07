"""Project-declared verification entries and complete monorepo discovery."""
import json
from pathlib import Path

from genesisai.core.extensions.tools.shell.python_support import (
    project_files, inspect_python_project, automatic_test_command, automatic_frontend_command,
    _read_pyproject, _read_text, _failure, _cancel, pytest_declarations, select_python, pytest_configuration_is_linked,
)
from genesisai.shared.security import ToolError


MANIFESTS = frozenset({'pyproject.toml', 'setup.cfg', 'setup.py', 'pytest.ini', 'tox.ini',
                       'package.json', 'Cargo.toml', 'go.mod', 'pom.xml', 'build.gradle', 'build.gradle.kts'})


def declared_commands(root, manifests, *, python=None):
    entries = []
    coverage = {'complete': True, 'errors': []}
    if python is None:
        config, _ = _read_pyproject(root, files=manifests, coverage=coverage)
        declarations = pytest_declarations(root, manifests, coverage, config)
    else:
        declarations = python['pytest_declarations']
    if declarations:
        executable = python['interpreter'] if python else str(select_python(root, coverage=coverage)[0])
        entries.append({'framework': 'pytest', 'command': [executable, '-m', 'pytest', '-q'],
                        'source': declarations[0]})
    if 'package.json' in manifests:
        text = _read_text(root, 'package.json', coverage)
        if text is not None:
            try:
                package = json.loads(text)
                if isinstance(package.get('scripts', {}).get('test'), str) and package['scripts']['test'].strip():
                    manager = str(package.get('packageManager', 'npm')).split('@', 1)[0]
                    if manager not in {'npm', 'pnpm', 'yarn', 'bun'}:
                        raise ValueError('packageManager 不受支持，请显式指定 command')
                    entries.append({'framework': 'npm', 'command': [manager, 'test'], 'source': 'package.json:scripts.test'})
            except (ValueError, TypeError, AttributeError) as exc:
                _failure(coverage, 'package.json', exc, 'parse')
    for manifest, framework, command in (
        ('Cargo.toml', 'cargo', ['cargo', 'test']), ('go.mod', 'go', ['go', 'test', './...']),
        ('pom.xml', 'mvn', ['mvn', 'test']), ('build.gradle', 'gradle', ['gradle', 'test']),
        ('build.gradle.kts', 'gradle', ['gradle', 'test']),
    ):
        if manifest in manifests and not any(item['framework'] == framework for item in entries):
            entries.append({'framework': framework, 'command': command, 'source': manifest})
    if any(name.endswith('.csproj') for name in manifests):
        entries.append({'framework': 'dotnet', 'command': ['dotnet', 'test'], 'source': '*.csproj'})
    return entries, coverage['errors']


def discover_project(root):
    root = Path(root).resolve()
    inventory = project_files(root)
    files, coverage = inventory
    details = inspect_python_project(root, inventory=inventory)
    grouped = {}
    for name in files:
        _cancel()
        path = Path(name)
        if path.name in MANIFESTS or path.name.endswith('.csproj'):
            grouped.setdefault(path.parent.as_posix(), []).append(path.name)
    packages, manifest_rows = [], []
    for path, manifests in sorted(grouped.items()):
        _cancel()
        commands, errors = declared_commands(root / path, manifests, python=details if path == '.' else None)
        packages.append({'path': path, 'manifest_count': len(manifests), 'test_commands': commands,
                         'error_count': len(errors)})
        manifest_rows.extend({'package': path, 'name': name} for name in manifests)
        for error in errors:
            item = {**error, 'path': (Path(path) / error['path']).as_posix()}
            if item not in coverage['errors']:
                coverage['errors'].append(item)
    coverage['complete'] = not coverage['errors']
    root_package = next((item for item in packages if item['path'] == '.'), None)
    details.update(package_count=len(packages), test_commands=root_package['test_commands'] if root_package else [],
                   nested_packages=sum(item['path'] != '.' for item in packages),
                   selection='显式 command 优先；根声明入口优先；无根入口时子包需明确选择，文件启发式只用于单项目')
    sections = {'packages': packages, 'manifests': manifest_rows, 'test_files': details['test_files'],
                'dependencies': details['dependencies'], 'source_roots': details['source_roots'],
                'html_files': [name for name in files if Path(name).suffix.lower() in {'.htm', '.html'}],
                'scan_errors': coverage['errors'], 'skipped_links': coverage['skipped_links']}
    return details, sections, inventory


def inspect_project(root, *, section='summary', offset=0, limit=100):
    details, sections, _ = discover_project(root)
    result = {key: value for key, value in details.items() if key not in {'test_files', 'dependencies', 'source_roots', 'coverage'}}
    result['coverage'] = {key: value for key, value in details['coverage'].items()
                          if key not in {'errors', 'skipped_links', 'directories'}}
    result['coverage'].update(error_count=len(sections['scan_errors']), skipped_link_count=len(sections['skipped_links']))
    result['sections'] = {name: len(values) for name, values in sections.items()}
    if section == 'summary':
        result['read_more'] = [{'section': name, 'offset': 0} for name, values in sections.items() if values]
    elif section in sections:
        values, items, size = sections[section], [], 0
        for value in values[offset:offset + limit]:
            encoded = len(json.dumps(value, ensure_ascii=False))
            if items and size + encoded > 16000:
                break
            items.append(value)
            size += encoded
        next_offset = offset + len(items)
        result.update(section=section, items=items, total=len(values),
                      next_offset=next_offset if next_offset < len(values) else None)
    else:
        raise ToolError('validation_error', '未知项目探测区域')
    return result


def automatic_project_command(root):
    root = Path(root).resolve()
    details, _, inventory = discover_project(root)
    entries = details['test_commands']
    if details['config_error'] or not details['coverage']['complete']:
        return [], details
    if len(entries) == 1:
        entry = entries[0]
        if entry['framework'] == 'pytest' and pytest_configuration_is_linked(details):
            return [], details
        details.update(test_framework=entry['framework'], test_detection_reason=entry['source'])
        return entry['command'], details
    if len(entries) > 1 or details['nested_packages']:
        return [], details
    command, details = automatic_test_command(root, details=details)
    if command or details['python_file_count']:
        return command, details
    command, frontend = automatic_frontend_command(root, inventory=inventory)
    if frontend is not None:
        details.update(frontend)
    return command, details
