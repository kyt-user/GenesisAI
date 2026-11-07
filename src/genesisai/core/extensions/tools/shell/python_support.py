"""开发工具共享的 Python 项目发现。"""

from __future__ import annotations

import json
import configparser
import os
import re
import sys
from pathlib import Path
import tomllib
from genesisai.shared.run_control import cancellation_requested
from genesisai.shared.security import ToolError


IGNORED_PARTS = frozenset({
    ".git", ".genesis", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", ".venv", "__pycache__", "build", "dist", "venv", 'node_modules', '.genesisai',
})


def _cancel():
    if cancellation_requested():
        raise ToolError('user_cancelled', '项目探测已取消')


def _failure(coverage, path, exc, operation):
    item = {'path': str(path), 'error': type(exc).__name__, 'operation': operation}
    if item not in coverage['errors']:
        coverage['errors'].append(item)
    coverage['complete'] = False


def project_files(root: Path):
    root = Path(root)
    files = []
    coverage = {'complete': True, 'excluded_directories': sorted(IGNORED_PARTS),
                'skipped_links': [], 'errors': [], 'directories': []}
    def linked(path):
        return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
    def failed(exc):
        _failure(coverage, exc.filename, exc, 'scan')
    for directory, folders, names in os.walk(root, followlinks=False, onerror=failed):
        _cancel()
        coverage['directories'].append(Path(directory).relative_to(root).as_posix())
        kept = []
        for name in sorted(folders):
            _cancel()
            if name in IGNORED_PARTS:
                continue
            path = Path(directory, name)
            try:
                if linked(path):
                    coverage['skipped_links'].append(path.relative_to(root).as_posix())
                else:
                    kept.append(name)
            except OSError as exc:
                _failure(coverage, path, exc, 'stat')
        folders[:] = kept
        for name in sorted(names):
            _cancel()
            if name.casefold() == '.env' or name.casefold().startswith('.env.'):
                continue
            path = Path(directory, name)
            try:
                if linked(path):
                    coverage['skipped_links'].append(path.relative_to(root).as_posix())
                elif path.is_file():
                    files.append(path.relative_to(root).as_posix())
            except OSError as exc:
                _failure(coverage, path, exc, 'stat')
    coverage.update(files_scanned=len(files), directories_scanned=len(coverage['directories']))
    return sorted(files), coverage


def _text_chunks(root, name, coverage):
    target = root / name
    try:
        for component in (target, *target.parents):
            if component == root:
                break
            if component.is_symlink() or bool(getattr(component.lstat(), 'st_file_attributes', 0) & 0x400):
                raise OSError('文件路径在扫描后变为链接')
        if not target.resolve().is_relative_to(root):
            raise OSError('文件在扫描后离开项目目录')
        with target.open(encoding='utf-8') as stream:
            while True:
                _cancel()
                chunk = stream.read(65536)
                if not chunk:
                    break
                yield chunk
    except ToolError:
        raise
    except (OSError, UnicodeError, RuntimeError) as exc:
        _failure(coverage, name, exc, 'read')


def _read_text(root, name, coverage):
    text = ''.join(_text_chunks(root, name, coverage))
    return None if any(item['path'] == name and item['operation'] == 'read' for item in coverage['errors']) else text


def _read_pyproject(root: Path, *, files, coverage) -> tuple[dict, str | None]:
    if 'pyproject.toml' not in files:
        return {}, None
    text = _read_text(root, 'pyproject.toml', coverage)
    if text is None:
        return {}, 'pyproject.toml 无法读取'
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        _failure(coverage, 'pyproject.toml', exc, 'parse')
        return {}, 'pyproject.toml 无法解析'
    return data, None


def pytest_declarations(root, files, coverage, pyproject):
    sources = []
    if isinstance(pyproject.get('tool'), dict) and 'pytest' in pyproject['tool']:
        sources.append('pyproject.toml:tool.pytest')
    for name, section in (('pytest.ini', 'pytest'), ('setup.cfg', 'tool:pytest'), ('tox.ini', 'pytest')):
        if name not in files:
            continue
        text = _read_text(root, name, coverage)
        if text is None:
            continue
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(text)
        except configparser.Error as exc:
            _failure(coverage, name, exc, 'parse')
            continue
        if parser.has_section(section) or (name == 'pytest.ini' and not text.strip()):
            sources.append(name + ':' + section)
    return sources


def select_python(root: Path, *, coverage=None) -> tuple[Path, str]:
    candidates = (
        (root / ".venv" / "Scripts" / "python.exe", ".venv"),
        (root / ".venv" / "bin" / "python", ".venv"),
        (root / "venv" / "Scripts" / "python.exe", "venv"),
        (root / "venv" / "bin" / "python", "venv"),
    )
    for candidate, source in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve(), source
        except (OSError, RuntimeError) as exc:
            if coverage is None:
                raise
            _failure(coverage, candidate, exc, 'interpreter')
    return Path(sys.executable).resolve(), "genesisai"


def _test_framework(root, declarations, test_files, files, coverage):
    if declarations:
        return 'pytest', declarations[0]
    if 'conftest.py' in files:
        return 'pytest', '发现 pytest 配置'
    found = set()
    pattern = re.compile(r'^[^\S\r\n]*(?:from[^\S\r\n]+(pytest|unittest)[^\S\r\n]+import\b|import[^\S\r\n]+(pytest|unittest)\b)')
    def observe(line):
        match = pattern.match(line)
        if match:
            found.add(match.group(1) or match.group(2))
    for name in test_files:
        pieces = []
        for chunk in _text_chunks(root, name, coverage):
            parts = chunk.split('\n')
            pieces.append(parts[0])
            if len(parts) == 1:
                continue
            observe(''.join(pieces))
            for line in parts[1:-1]:
                _cancel()
                observe(line)
            pieces = [parts[-1]]
        _cancel()
        observe(''.join(pieces))
    if 'pytest' in found:
        return 'pytest', '测试代码导入 pytest'
    if 'unittest' in found:
        return 'unittest', '测试代码使用 unittest'
    if test_files:
        return 'pytest', '发现 Python 测试文件'
    return 'compileall', '未发现测试，使用语法编译检查'


def inspect_python_project(root: Path, *, inventory=None) -> dict:
    root = root.resolve()
    files, coverage = project_files(root) if inventory is None else inventory
    pyproject, config_error = _read_pyproject(root, files=files, coverage=coverage)
    project = pyproject.get('project') if isinstance(pyproject.get('project'), dict) else {}
    python_files = [name for name in files if name.endswith('.py')]
    test_files = [name for name in python_files if Path(name).name.startswith('test_')
                  or Path(name).name.endswith('_test.py') or 'tests' in Path(name).parts]
    tests = set(test_files)
    source_files = [name for name in python_files if name not in tests]
    declarations = pytest_declarations(root, files, coverage, pyproject)
    interpreter, interpreter_source = select_python(root, coverage=coverage)
    framework, reason = _test_framework(root, declarations, test_files, files, coverage)
    directories = coverage['directories']
    source_roots = ['src'] if 'src' in directories else []
    source_roots.extend(name for name in sorted(directories)
                        if name not in {'.', 'src'} and '/' not in name and name + '/__init__.py' in files)
    if not source_roots and source_files:
        source_roots.append('.')
    dependencies = project.get('dependencies', [])
    if not isinstance(dependencies, list):
        dependencies = []
    return {
        'root': str(root), 'name': project.get('name') if isinstance(project.get('name'), str) else root.name,
        'requires_python': project.get('requires-python') if isinstance(project.get('requires-python'), str) else None,
        'manifests': [name for name in ('pyproject.toml', 'setup.cfg', 'setup.py', 'requirements.txt', 'pytest.ini', 'tox.ini') if name in files],
        'source_roots': source_roots, 'python_file_count': len(python_files),
        'source_file_count': len(source_files), 'test_file_count': len(test_files), 'test_files': test_files,
        'dependencies': [item for item in dependencies if isinstance(item, str)],
        'interpreter': str(interpreter), 'interpreter_source': interpreter_source,
        'test_framework': framework, 'test_detection_reason': reason,
        'pytest_declarations': declarations, 'config_error': config_error, 'coverage': coverage,
    }


def pytest_configuration_is_linked(details):
    names = {'pytest.ini', '.pytest.ini', 'pyproject.toml', 'setup.cfg', 'tox.ini', 'conftest.py'}
    linked = sorted(names.intersection(details['coverage']['skipped_links']))
    if linked:
        details['selection_issue'] = 'pytest会读取已跳过的根配置链接：' + ', '.join(linked) + '；请明确提供command或修正配置链接'
    return bool(linked)


def automatic_test_command(root: Path, *, details=None) -> tuple[list[str], dict]:
    details = inspect_python_project(root) if details is None else details
    if details['python_file_count'] == 0 or not details['coverage']['complete']:
        return [], details
    interpreter, framework = details['interpreter'], details['test_framework']
    if framework == 'pytest':
        if pytest_configuration_is_linked(details):
            return [], details
        return [interpreter, '-m', 'pytest', '-q'], details
    if framework == 'unittest':
        start = 'tests' if 'tests' in details['coverage']['directories'] else '.'
        return [interpreter, '-m', 'unittest', 'discover', '-s', start, '-p', 'test*.py'], details
    targets = [item for item in details['source_roots'] if item != '.'] or ['.']
    if any(target == '.' or link == target or link.startswith(target + '/')
           for target in targets for link in details['coverage']['skipped_links']):
        details['selection_issue'] = 'compileall 会重新遍历已跳过的链接；请显式选择不含链接的测试目标'
        return [], details
    return [interpreter, '-m', 'compileall', '-q', *targets], details


def automatic_frontend_command(root: Path, *, inventory=None) -> tuple[list[str], dict | None]:
    files, coverage = project_files(root) if inventory is None else inventory
    html_files = [name for name in files if Path(name).suffix.lower() in {'.htm', '.html'}]
    if not html_files or not coverage['complete']:
        return [], None
    return [str(Path(sys.executable).resolve()), '-m', 'genesisai.core.extensions.tools.shell.static_validate', '.'], {
        'test_framework': 'frontend_static',
        'test_detection_reason': '发现 HTML 文件，执行结构和内联脚本静态检查',
        'html_files': html_files,
    }


def parse_test_summary(framework: str, stdout: str, stderr: str, exit_code: int) -> dict:
    text = stdout + "\n" + stderr
    summary = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    if framework == "pytest":
        for label, key in (("passed", "passed"), ("failed", "failed"), ("error", "errors"), ("skipped", "skipped")):
            match = re.search(rf"(\d+)\s+{label}s?\b", text, re.I)
            if match:
                summary[key] = int(match.group(1))
    elif framework == "unittest":
        ran = re.search(r"Ran\s+(\d+)\s+tests?", text)
        if ran and exit_code == 0:
            summary["passed"] = int(ran.group(1))
        failures = re.search(r"failures=(\d+)", text)
        errors = re.search(r"errors=(\d+)", text)
        summary["failed"] = int(failures.group(1)) if failures else 0
        summary["errors"] = int(errors.group(1)) if errors else 0
    elif framework in {"npm", "cargo", "go", "dotnet", "mvn", "gradle"}:
        markers = re.findall(r"\b(\d+) (?:passed|passing)\b|^\s*# pass (\d+)|\bPassed:\s*(\d+)", text, re.M | re.I)
        summary["passed"] = sum(int(next(filter(None, m))) for m in markers)
        if framework == "go":
            summary["passed"] = len(re.findall(r"^ok\s+(?!.*\[no tests to run\]).+$", text, re.M))
        if framework == "mvn":
            for ran, failures, errors, skipped in re.findall(
                    r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)", text):
                summary["passed"] = max(summary["passed"], int(ran)-int(failures)-int(errors)-int(skipped))
    elif framework == "compileall":
        summary["passed"] = 1 if exit_code == 0 else 0
        summary["errors"] = 0 if exit_code == 0 else 1
    elif framework == "frontend_static":
        try:
            payload = json.loads(next(line for line in reversed(stdout.splitlines()) if line.strip()))
        except (StopIteration, json.JSONDecodeError, TypeError):
            payload = {}
        summary["passed"] = int(payload.get("checked", 0)) if exit_code == 0 else 0
        summary["errors"] = len(payload.get("errors", [])) if isinstance(payload.get("errors"), list) else int(exit_code != 0)
    return summary
