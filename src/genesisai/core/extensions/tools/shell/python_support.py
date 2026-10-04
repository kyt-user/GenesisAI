"""开发工具共享的 Python 项目发现。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
import tomllib


IGNORED_PARTS = frozenset({
    ".git", ".genesis", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".tox", ".venv", "agent_docs", "__pycache__", "build", "dist", "venv",
})


def _relative_files(root: Path, pattern: str, *, limit: int = 5000) -> list[str]:
    values = []
    for path in root.rglob(pattern):
        relative = path.relative_to(root)
        if path.is_file() and not any(part in IGNORED_PARTS for part in relative.parts):
            values.append(relative.as_posix())
            if len(values) >= limit:
                break
    return sorted(values)


def _read_pyproject(root: Path) -> tuple[dict, str | None]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}, None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        return {}, f"{type(exc).__name__}: pyproject.toml 无法解析"
    return data if isinstance(data, dict) else {}, None


def select_python(root: Path) -> tuple[Path, str]:
    candidates = (
        (root / ".venv" / "Scripts" / "python.exe", ".venv"),
        (root / ".venv" / "bin" / "python", ".venv"),
        (root / "venv" / "Scripts" / "python.exe", "venv"),
        (root / "venv" / "bin" / "python", "venv"),
    )
    for candidate, source in candidates:
        if candidate.is_file():
            return candidate.resolve(), source
    return Path(sys.executable).resolve(), "genesisai"


def _test_framework(root: Path, pyproject: dict, test_files: list[str]) -> tuple[str, str]:
    tool = pyproject.get("tool") if isinstance(pyproject.get("tool"), dict) else {}
    if isinstance(tool, dict) and "pytest" in tool:
        return "pytest", "pyproject.toml 声明了 tool.pytest"
    if any((root / name).is_file() for name in ("pytest.ini", "conftest.py")):
        return "pytest", "发现 pytest 配置"
    sample = ""
    for name in test_files[:40]:
        try:
            sample += (root / name).read_text(encoding="utf-8", errors="replace")[:8000]
        except OSError:
            continue
    if re.search(r"(^|\n)\s*(?:from\s+pytest\s+import|import\s+pytest\b)", sample):
        return "pytest", "测试代码导入 pytest"
    if test_files and re.search(r"(^|\n)\s*(?:from\s+unittest\s+import|import\s+unittest\b)", sample):
        return "unittest", "测试代码使用 unittest"
    if test_files:
        return "pytest", "发现 Python 测试文件"
    return "compileall", "未发现测试，使用语法编译检查"


def inspect_python_project(root: Path) -> dict:
    root = root.resolve()
    pyproject, config_error = _read_pyproject(root)
    project = pyproject.get("project") if isinstance(pyproject.get("project"), dict) else {}
    python_files = _relative_files(root, "*.py")
    test_files = [
        name for name in python_files
        if Path(name).name.startswith("test_") or Path(name).name.endswith("_test.py")
        or "tests" in Path(name).parts
    ]
    source_files = [name for name in python_files if name not in test_files]
    interpreter, interpreter_source = select_python(root)
    framework, reason = _test_framework(root, pyproject, test_files)
    source_roots = []
    if (root / "src").is_dir():
        source_roots.append("src")
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "__init__.py").is_file() and child.name not in IGNORED_PARTS:
            source_roots.append(child.name)
    if not source_roots and source_files:
        source_roots.append(".")
    manifests = [
        name for name in ("pyproject.toml", "setup.cfg", "setup.py", "requirements.txt", "pytest.ini", "tox.ini")
        if (root / name).is_file()
    ]
    dependencies = project.get("dependencies", []) if isinstance(project, dict) else []
    if not isinstance(dependencies, list):
        dependencies = []
    return {
        "root": str(root),
        "name": project.get("name") if isinstance(project.get("name"), str) else root.name,
        "requires_python": project.get("requires-python") if isinstance(project.get("requires-python"), str) else None,
        "manifests": manifests,
        "source_roots": source_roots,
        "python_file_count": len(python_files),
        "source_file_count": len(source_files),
        "test_file_count": len(test_files),
        "test_files": test_files[:100],
        "dependencies": [item for item in dependencies[:100] if isinstance(item, str)],
        "interpreter": str(interpreter),
        "interpreter_source": interpreter_source,
        "test_framework": framework,
        "test_detection_reason": reason,
        "config_error": config_error,
    }


def automatic_test_command(root: Path) -> tuple[list[str], dict]:
    details = inspect_python_project(root)
    if details["python_file_count"] == 0:
        return [], details
    interpreter = details["interpreter"]
    framework = details["test_framework"]
    if framework == "pytest":
        return [interpreter, "-m", "pytest", "-q"], details
    if framework == "unittest":
        start = "tests" if (root / "tests").is_dir() else "."
        return [interpreter, "-m", "unittest", "discover", "-s", start, "-p", "test*.py"], details
    targets = [item for item in details["source_roots"] if item != "."] or ["."]
    return [interpreter, "-m", "compileall", "-q", *targets], details


def automatic_frontend_command(root: Path) -> tuple[list[str], dict | None]:
    html_files = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.htm*")
        if path.is_file() and not any(part in IGNORED_PARTS for part in path.relative_to(root).parts)
    ]
    if not html_files:
        return [], None
    validator = Path(__file__).with_name("static_validate.py").resolve()
    return [str(Path(sys.executable).resolve()), str(validator), "."], {
        "test_framework": "frontend_static",
        "test_detection_reason": "发现 HTML 文件，执行结构和内联脚本静态检查",
        "html_files": sorted(html_files)[:100],
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
